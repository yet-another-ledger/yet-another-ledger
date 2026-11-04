"""M17 Step 1 static PDF validation and exact-original tests."""

from __future__ import annotations

import asyncio
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from starlette.requests import Request

from jai.api.pdf import (
    _ArtifactMultipartError,
    _PdfAppendixTooLargeError,
    _read_pdf_appendix_multipart,
)
from jai.services.pdf_appendix import (
    PdfAppendixValidationError,
    _BoundedPdfBuffer,
    _reject_signed_or_interactive_objects,
    _validate_and_copy,
)


def _pdf(
    *,
    dangerous_action: bool = False,
    chained_uri: bool = False,
    encrypted: bool = False,
    form: bool = False,
    catalog_action: bool = False,
    page_action: bool = False,
    empty_password_encryption: bool = False,
    unsafe_uri: bool = False,
) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=400, height=700)
    if dangerous_action or chained_uri or unsafe_uri:
        page = writer.pages[0]
        if chained_uri:
            followup = DictionaryObject({NameObject("/S"): NameObject("/JavaScript")})
            action = DictionaryObject(
                {
                    NameObject("/S"): NameObject("/URI"),
                    NameObject("/URI"): TextStringObject("https://example.com/guide"),
                    NameObject("/Next"): writer._add_object(followup),
                }
            )
        elif unsafe_uri:
            action = DictionaryObject(
                {
                    NameObject("/S"): NameObject("/URI"),
                    NameObject("/URI"): TextStringObject("file:///etc/passwd"),
                }
            )
        else:
            action = DictionaryObject({NameObject("/S"): NameObject("/JavaScript")})
        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/A"): writer._add_object(action),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    if form:
        writer.root_object[NameObject("/AcroForm")] = writer._add_object(
            DictionaryObject({NameObject("/Fields"): ArrayObject()})
        )
    if catalog_action:
        writer.root_object[NameObject("/OpenAction")] = writer._add_object(
            DictionaryObject({NameObject("/S"): NameObject("/Launch")})
        )
    if page_action:
        writer.pages[0][NameObject("/AA")] = writer._add_object(DictionaryObject())
    if encrypted:
        writer.encrypt("" if empty_password_encryption else "test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _pdf_with_custom_info_metadata(*, key: str) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            key: "Product brochure",
            "/CatalogLabel": "Ordinary static document",
        }
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _signed_pdf(
    *, escaped_name: bool = False, indirect: bool = False, comment_after_name: bool = False
) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    byte_range = NameObject("/Byte#52ange") if escaped_name else NameObject("/ByteRange")
    signature = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Sig"),
            NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
            byte_range: ArrayObject(
                [NumberObject(0), NumberObject(1), NumberObject(2), NumberObject(3)]
            ),
            NameObject("/Contents"): ByteStringObject(b"test"),
        }
    )
    perms = DictionaryObject(
        {NameObject("/DocMDP"): writer._add_object(signature) if indirect else signature}
    )
    writer.root_object[NameObject("/Perms")] = writer._add_object(perms)
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    if comment_after_name:
        encoded_name = b"/Byte#52ange" if escaped_name else b"/ByteRange"
        content = content.replace(encoded_name, encoded_name + b" % allowed PDF comment\n  ")
    return content


def _three_d_pdf(*, indirect: bool = False, shared: bool = False, cyclic: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    javascript = DecodedStreamObject()
    javascript.set_data(b"/* inert */")
    three_d = DecodedStreamObject()
    three_d.set_data(b"U3D")
    three_d[NameObject("/Type")] = NameObject("/3D")
    three_d[NameObject("/Subtype")] = NameObject("/U3D")
    three_d[NameObject("/OnInstantiate")] = (
        writer._add_object(javascript) if indirect else javascript
    )
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/3D"),
            NameObject("/3DD"): writer._add_object(three_d) if indirect else three_d,
        }
    )
    annotation_ref = writer._add_object(annotation)
    if cyclic:
        annotation[NameObject("/Cycle")] = annotation_ref
    page[NameObject("/Annots")] = ArrayObject(
        [annotation_ref, annotation_ref if shared else writer._add_object(annotation)]
    )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _sound_pdf(*, indirect_type_values: bool) -> bytes:
    """Build a complete one-channel 8-bit PCM sound annotation PDF."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    sound = DecodedStreamObject()
    sound.set_data(bytes(range(256)) * 4)
    sound_values = {
        NameObject("/Type"): NameObject("/Sound"),
        NameObject("/R"): NumberObject(8000),
        NameObject("/C"): NumberObject(1),
        NameObject("/B"): NumberObject(8),
        NameObject("/E"): NameObject("/Raw"),
    }
    for key, value in sound_values.items():
        sound[key] = writer._add_object(value) if indirect_type_values else value
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
            NameObject("/Subtype"): (
                writer._add_object(NameObject("/Sound"))
                if indirect_type_values
                else NameObject("/Sound")
            ),
            NameObject("/Name"): NameObject("/Speaker"),
            NameObject("/Sound"): writer._add_object(sound),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _uri_with_indirect_values_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    action = DictionaryObject(
        {
            NameObject("/S"): writer._add_object(NameObject("/URI")),
            NameObject("/URI"): writer._add_object(TextStringObject("https://example.com/guide")),
        }
    )
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/A"): writer._add_object(action),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_valid_multipage_pdf_preserves_original_hash_and_size() -> None:
    content = _pdf()
    result = _validate_and_copy(content, "application/pdf", 100_000, 10)
    import hashlib

    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert result.page_count == 2


@pytest.mark.parametrize("key", ["/Type", "/Subtype"])
def test_custom_text_in_document_info_is_valid_and_preserves_original(key: str) -> None:
    import hashlib

    content = _pdf_with_custom_info_metadata(key=key)
    result = _validate_and_copy(content, "application/pdf", 100_000, 10)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert result.page_count == 1
    page = PdfReader(io.BytesIO(content), strict=True).pages[0]
    assert float(page.mediabox.width) == 612
    assert float(page.mediabox.height) == 792


@pytest.mark.parametrize("info_first", [False, True])
def test_shared_info_dictionary_is_rechecked_in_structural_context(info_first: bool) -> None:
    shared = {"/Type": TextStringObject("Product brochure")}
    structure = {"/Perms": {"/DocMDP": shared}}
    trailer = (
        {"/Info": shared, "/Root": structure}
        if info_first
        else {"/Root": structure, "/Info": shared}
    )
    with pytest.raises(PdfAppendixValidationError):
        _reject_signed_or_interactive_objects(trailer, set(), trailer_object=trailer)


def test_shared_indirect_info_dictionary_is_rechecked_as_structure() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    info_ref = writer._info
    info = info_ref.get_object()
    info[NameObject("/Type")] = TextStringObject("Product brochure")
    writer.root_object[NameObject("/Perms")] = writer._add_object(
        DictionaryObject({NameObject("/DocMDP"): info_ref})
    )
    output = io.BytesIO()
    writer.write(output)

    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)


@pytest.mark.parametrize(
    "content,mime",
    [
        (b"", "application/pdf"),
        (b"<html>not pdf</html>", "application/pdf"),
        (b"%PDF-broken", "application/pdf"),
        (_pdf(), "text/plain"),
        (_pdf() + b"\n/ByteRange [0 1 2 3]", "application/pdf"),
        (_pdf(encrypted=True), "application/pdf"),
        (_pdf(encrypted=True, empty_password_encryption=True), "application/pdf"),
        (_pdf(form=True), "application/pdf"),
        (_pdf(dangerous_action=True), "application/pdf"),
        (_pdf(catalog_action=True), "application/pdf"),
        (_pdf(page_action=True), "application/pdf"),
        (_pdf(unsafe_uri=True), "application/pdf"),
        (_signed_pdf(), "application/pdf"),
        (_signed_pdf(escaped_name=True), "application/pdf"),
        (_signed_pdf(escaped_name=True, indirect=True), "application/pdf"),
        (_signed_pdf(escaped_name=True, indirect=True, comment_after_name=True), "application/pdf"),
        (_three_d_pdf(), "application/pdf"),
        (_three_d_pdf(indirect=True), "application/pdf"),
        (_three_d_pdf(indirect=True, shared=True, cyclic=True), "application/pdf"),
        (_sound_pdf(indirect_type_values=False), "application/pdf"),
        (_sound_pdf(indirect_type_values=True), "application/pdf"),
    ],
)
def test_unsupported_or_unsafe_pdf_is_rejected(content: bytes, mime: str) -> None:
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(content, mime, 100_000, 10)


def test_size_and_page_limits_are_enforced() -> None:
    content = _pdf()
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(content, "application/pdf", len(content) - 1, 10)
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(content, "application/pdf", 100_000, 1)


@pytest.mark.parametrize("indirect", [False, True])
def test_page_associated_embedded_file_is_rejected(indirect: bool) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    stream = DecodedStreamObject()
    stream.set_data(b"secret")
    stream[NameObject("/Type")] = NameObject("/EmbeddedFile")
    filespec = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Filespec"),
            NameObject("/EF"): DictionaryObject({NameObject("/F"): writer._add_object(stream)}),
        }
    )
    page[NameObject("/AF")] = (
        writer._add_object(ArrayObject([writer._add_object(filespec)]))
        if indirect
        else ArrayObject([filespec])
    )
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)


@pytest.mark.parametrize(
    "pdf",
    [
        _signed_pdf(),
        _signed_pdf(escaped_name=True),
        _signed_pdf(escaped_name=True, indirect=True),
        _signed_pdf(escaped_name=False, comment_after_name=True),
        _signed_pdf(escaped_name=True, indirect=True, comment_after_name=True),
    ],
)
def test_catalog_signature_declarations_are_rejected_from_parsed_objects(pdf: bytes) -> None:
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(pdf, "application/pdf", 100_000, 10)


@pytest.mark.parametrize(
    "pdf",
    [
        _three_d_pdf(),
        _three_d_pdf(indirect=True),
        _three_d_pdf(indirect=True, shared=True, cyclic=True),
    ],
)
def test_3d_script_stream_graphs_are_rejected(pdf: bytes) -> None:
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(pdf, "application/pdf", 100_000, 10)


@pytest.mark.parametrize(
    "key,value",
    [
        ("/Subtype", "/3D"),
        ("/Subtype", "/RichMedia"),
        ("/Subtype", "/Movie"),
        ("/Subtype", "/Screen"),
        ("/Type", "/3D"),
        ("/FT", "/Sig"),
    ],
)
def test_indirect_media_and_signature_type_names_are_rejected(key: str, value: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject(key): writer._add_object(NameObject(value)),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)


def test_cyclic_indirect_semantic_value_is_rejected_without_hanging() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
        }
    )
    annotation_ref = writer._add_object(annotation)
    annotation[NameObject("/Subtype")] = annotation_ref
    page[NameObject("/Annots")] = ArrayObject([annotation_ref])
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)


def test_static_uri_with_indirect_action_values_is_preserved() -> None:
    result = _validate_and_copy(_uri_with_indirect_values_pdf(), "application/pdf", 100_000, 10)
    assert result.page_count == 1


@pytest.mark.asyncio
async def test_cancelled_pdf_validation_holds_worker_permit_until_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import jai.services.pdf_appendix as service

    gate = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def blocked(*args: object, **kwargs: object) -> object:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        gate.wait(0.7)
        with lock:
            state["active"] -= 1
        return None

    monkeypatch.setattr(service, "_validate_and_copy", blocked)
    tasks = [
        asyncio.create_task(service.validate_pdf(b"x", "application/pdf", max_bytes=1, max_pages=1))
        for _ in range(10)
    ]
    for _ in range(100):
        with lock:
            active = state["active"]
        if active >= 4:
            break
        await asyncio.sleep(0.005)
    assert active == 4
    for task in tasks:
        task.cancel()
        await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.005)
    await asyncio.gather(*tasks, return_exceptions=True)
    with lock:
        assert state["peak"] <= 4 and state["active"] == 4
    for _ in range(200):
        with lock:
            active = state["active"]
        if active == 0:
            break
        await asyncio.sleep(0.01)
    assert active == 0


@pytest.mark.asyncio
async def test_pdf_worker_exception_releases_concurrency_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jai.services.pdf_appendix as service

    calls = 0

    def failing(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("worker failed")
        return None

    monkeypatch.setattr(service, "_validate_and_copy", failing)
    with pytest.raises(RuntimeError):
        await service.validate_pdf(b"x", "application/pdf", max_bytes=1, max_pages=1)
    assert await service.validate_pdf(b"x", "application/pdf", max_bytes=1, max_pages=1) is None


@pytest.mark.asyncio
async def test_cancelled_pdf_parser_failure_is_consumed_without_loop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import jai.services.pdf_appendix as service

    data = (
        b"%PDF-1.7\n1 0 obj\n<< /Type /M17_CANCEL_PARSER_SECRET /Size 1 /W [1 1 1] "
        b"/Length 0 >>\nstream\n\nendstream\nendobj\nstartxref\n9\n%%EOF\n"
    )
    started = threading.Event()
    release_parser = threading.Event()
    finished = threading.Event()
    original = service._validate_and_copy

    def delayed_parser(*args: object, **kwargs: object) -> object:
        started.set()
        assert release_parser.wait(2)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    contexts: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    monkeypatch.setattr(service, "_validate_and_copy", delayed_parser)
    task = asyncio.create_task(
        service.validate_pdf(data, "application/pdf", max_bytes=100_000, max_pages=10)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    assert started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release_parser.set()
    for _ in range(100):
        if finished.is_set():
            break
        await asyncio.sleep(0.005)
    assert finished.is_set()
    # Let the worker done callback consume the parser failure and release its permit.
    await asyncio.sleep(0)
    assert contexts == []
    assert await service.validate_pdf(_pdf(), "application/pdf", max_bytes=100_000, max_pages=10)


@pytest.mark.parametrize("action_key", ["/NA", "/PA"])
@pytest.mark.parametrize("action_kind", ["/JavaScript", "/Launch"])
@pytest.mark.parametrize("indirect_action", [False, True])
def test_page_navigation_actions_are_rejected(
    action_key: str, action_kind: str, indirect_action: bool
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    action = DictionaryObject({NameObject("/S"): NameObject(action_kind)})
    if action_kind == "/JavaScript":
        action[NameObject("/JS")] = TextStringObject("app.alert('not executed')")
    else:
        action[NameObject("/F")] = TextStringObject("not-executed.pdf")
    action_value = writer._add_object(action) if indirect_action else action
    node = DictionaryObject(
        {NameObject("/Type"): NameObject("/NavNode"), NameObject(action_key): action_value}
    )
    page[NameObject("/PresSteps")] = writer._add_object(node)
    output = io.BytesIO()
    writer.write(output)

    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)


def test_navigation_action_scan_handles_shared_and_cyclic_page_objects() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    node = DictionaryObject()
    node_ref = writer._add_object(node)
    node[NameObject("/Type")] = NameObject("/NavNode")
    node[NameObject("/Self")] = node_ref
    safe_action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://example.com/guide"),
        }
    )
    action_ref = writer._add_object(safe_action)
    node[NameObject("/NA")] = action_ref
    node[NameObject("/PA")] = action_ref
    page[NameObject("/PresSteps")] = node_ref
    output = io.BytesIO()
    writer.write(output)

    assert _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10).page_count == 1


def test_safe_uri_link_is_retained_but_uri_next_action_is_rejected() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    action = DictionaryObject(
        {
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://example.com/manual"),
        }
    )
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/A"): writer._add_object(action),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    output = io.BytesIO()
    writer.write(output)
    result = _validate_and_copy(output.getvalue(), "application/pdf", 100_000, 10)
    assert result.page_count == 1
    with pytest.raises(PdfAppendixValidationError):
        _validate_and_copy(_pdf(chained_uri=True), "application/pdf", 100_000, 10)


def test_rewritten_candidate_buffer_has_a_hard_byte_limit() -> None:
    buffer = _BoundedPdfBuffer(4)
    assert buffer.write(b"1234") == 4
    with pytest.raises(PdfAppendixValidationError):
        buffer.write(b"5")
    buffer.close()


def _multipart_request(body: bytes, *, content_length: str | None = None) -> Request:
    boundary = b"m17-boundary"
    chunks = [body[i : i + 73] for i in range(0, len(body), 73)]
    state = {"index": 0}

    async def receive() -> dict[str, object]:
        index = state["index"]
        state["index"] += 1
        if index >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    headers = [(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    return Request(
        {"type": "http", "method": "POST", "path": "/api/v1/pdf-library", "headers": headers},
        receive,
    )


def test_bounded_chunked_multipart_accepts_file_and_title_without_trusting_length() -> None:
    content = _pdf()
    body = (
        b'--m17-boundary\r\nContent-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        + content
        + b'\r\n--m17-boundary\r\nContent-Disposition: form-data; name="title"\r\n\r\n'
        b"Manual\r\n--m17-boundary--\r\n"
    )
    result = asyncio.run(
        _read_pdf_appendix_multipart(
            _multipart_request(body, content_length="1"), max_bytes=100_000
        )
    )
    assert result == (content, "application/pdf", "doc.pdf", "Manual")


def test_bounded_chunked_multipart_rejects_oversize_and_bad_framing() -> None:
    content = _pdf()
    prefix = (
        b'--m17-boundary\r\nContent-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
    )
    oversized = prefix + content + b"x" * 500 + b"\r\n--m17-boundary--\r\n"
    with pytest.raises(_PdfAppendixTooLargeError):
        asyncio.run(
            _read_pdf_appendix_multipart(_multipart_request(oversized), max_bytes=len(content))
        )
    with pytest.raises(_ArtifactMultipartError):
        asyncio.run(
            _read_pdf_appendix_multipart(_multipart_request(prefix + content), max_bytes=100_000)
        )
    bad_header = (
        b'--m17-boundary\r\nContent-Disposition: form-data; name="file"\r\n\r\n'
        + content
        + b"\r\n--m17-boundary--\r\n"
    )
    with pytest.raises(_ArtifactMultipartError):
        asyncio.run(_read_pdf_appendix_multipart(_multipart_request(bad_header), max_bytes=100_000))
