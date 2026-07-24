"""Tests for :mod:`qtea.context_images` and the operator context-image plumbing.

Covers format/size/content validation, media-type resolution, base64 encoding,
checkpoint round-trip, and the pipeline's workspace-copy materialization.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from qtea import context_images as ci
from qtea.checkpoints import RunState

# Minimal magic-byte-valid image payloads (only the first ~12 bytes matter).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_GIF = b"GIF89a" + b"\x00" * 16
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# media_type_for / sniff_media_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("a.PNG", "image/png"),
        ("a.bmp", None),
        ("a.svg", None),
        ("noext", None),
    ],
)
def test_media_type_for(name, expected):
    assert ci.media_type_for(name) == expected


@pytest.mark.parametrize(
    "data,expected",
    [
        (_PNG, "image/png"),
        (_JPEG, "image/jpeg"),
        (_GIF, "image/gif"),
        (_WEBP, "image/webp"),
        (b"not an image", None),
        (b"", None),
    ],
)
def test_sniff_media_type(data, expected):
    assert ci.sniff_media_type(data) == expected


# ---------------------------------------------------------------------------
# validate_image_file
# ---------------------------------------------------------------------------

def test_validate_accepts_valid_png(tmp_path):
    p = _write(tmp_path, "shot.png", _PNG)
    assert ci.validate_image_file(p) == "image/png"


def test_validate_rejects_unsupported_extension(tmp_path):
    p = _write(tmp_path, "diagram.bmp", _PNG)  # png bytes, bmp name
    with pytest.raises(ci.ContextImageError, match="unsupported format"):
        ci.validate_image_file(p)


def test_validate_rejects_mislabeled_content(tmp_path):
    # .png extension but the bytes are not a real image.
    p = _write(tmp_path, "fake.png", b"this is plain text, not a png")
    with pytest.raises(ci.ContextImageError, match="not a valid"):
        ci.validate_image_file(p)


def test_validate_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "MAX_CONTEXT_IMAGE_BYTES", 8)
    p = _write(tmp_path, "big.png", _PNG)  # >8 bytes
    with pytest.raises(ci.ContextImageError, match="exceeds"):
        ci.validate_image_file(p)


def test_validate_rejects_missing_file(tmp_path):
    with pytest.raises(ci.ContextImageError):
        ci.validate_image_file(tmp_path / "nope.png")


# ---------------------------------------------------------------------------
# encode_image_block
# ---------------------------------------------------------------------------

def test_encode_image_block_shape_and_data(tmp_path):
    p = _write(tmp_path, "shot.jpg", _JPEG)
    block = ci.encode_image_block(p)
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(block["source"]["data"]) == _JPEG


def test_encode_image_block_rejects_bad_file(tmp_path):
    p = _write(tmp_path, "x.gif", b"nope")
    with pytest.raises(ci.ContextImageError):
        ci.encode_image_block(p)


# ---------------------------------------------------------------------------
# Checkpoint round-trip (paths only, never base64)
# ---------------------------------------------------------------------------

def test_runstate_roundtrips_context_images():
    rs = RunState(
        run_id="r1",
        workspace="/ws",
        spec_source="s",
        sut_source="u",
        operator_context="ctx",
        operator_context_images=["operator-context/images/a.png"],
    )
    d = rs.to_dict()
    assert d["operator_context_images"] == ["operator-context/images/a.png"]
    rt = RunState.from_dict(d)
    assert rt.operator_context_images == ["operator-context/images/a.png"]


def test_runstate_from_dict_defaults_empty_when_absent():
    rt = RunState.from_dict(
        {"run_id": "r", "workspace": "/w", "spec_source": None, "sut_source": None}
    )
    assert rt.operator_context_images == []


# ---------------------------------------------------------------------------
# Pipeline materialization (copy into workspace + resume reuse)
# ---------------------------------------------------------------------------

class _Ws:
    def __init__(self, root: Path):
        self.root = root


class _Log:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, _event, **kw):
        self.warnings.append(kw.get("reason", ""))


def _opts(images):
    from qtea.pipeline import PipelineOptions

    return PipelineOptions(workspace_base=Path("."), operator_context_images=images)


def test_materialize_copies_and_records_relative_paths(tmp_path):
    from qtea.pipeline import _materialize_context_images

    src = _write(tmp_path / "src", "shot.png", _PNG)
    ws = _Ws(tmp_path / "ws")
    ws.root.mkdir()
    state = RunState(run_id="r", workspace=str(ws.root), spec_source=None, sut_source=None)

    abs_paths = _materialize_context_images(_opts([str(src)]), ws, state, _Log())

    assert len(abs_paths) == 1
    assert abs_paths[0].is_file()
    assert abs_paths[0].read_bytes() == _PNG
    assert state.operator_context_images == ["operator-context/images/shot.png"]


def test_materialize_caps_at_max(tmp_path):
    from qtea.pipeline import _materialize_context_images

    srcs = [str(_write(tmp_path, f"s{i}.png", _PNG)) for i in range(ci.MAX_CONTEXT_IMAGES + 3)]
    ws = _Ws(tmp_path / "ws")
    ws.root.mkdir()
    state = RunState(run_id="r", workspace=str(ws.root), spec_source=None, sut_source=None)

    abs_paths = _materialize_context_images(_opts(srcs), ws, state, _Log())

    assert len(abs_paths) == ci.MAX_CONTEXT_IMAGES


def test_materialize_skips_invalid_and_dedupes_names(tmp_path):
    from qtea.pipeline import _materialize_context_images

    good = str(_write(tmp_path / "a", "shot.png", _PNG))
    bad = str(_write(tmp_path / "b", "shot.png", b"garbage"))  # same name, invalid
    (tmp_path / "a").mkdir(exist_ok=True)
    ws = _Ws(tmp_path / "ws")
    ws.root.mkdir()
    state = RunState(run_id="r", workspace=str(ws.root), spec_source=None, sut_source=None)
    log = _Log()

    abs_paths = _materialize_context_images(_opts([good, bad]), ws, state, log)

    assert len(abs_paths) == 1  # bad one skipped
    assert log.warnings  # a skip was recorded


def test_materialize_resume_reuses_recorded_paths(tmp_path):
    from qtea.pipeline import _materialize_context_images

    ws = _Ws(tmp_path / "ws")
    img_dir = ws.root / "operator-context" / "images"
    img_dir.mkdir(parents=True)
    (img_dir / "shot.png").write_bytes(_PNG)
    state = RunState(
        run_id="r",
        workspace=str(ws.root),
        spec_source=None,
        sut_source=None,
        operator_context_images=["operator-context/images/shot.png"],
    )

    # Resume path: opts.operator_context_images is None.
    abs_paths = _materialize_context_images(_opts(None), ws, state, _Log())

    assert len(abs_paths) == 1
    assert abs_paths[0].read_bytes() == _PNG
    assert state.operator_context_images == ["operator-context/images/shot.png"]


def test_materialize_resume_drops_missing_files(tmp_path):
    from qtea.pipeline import _materialize_context_images

    ws = _Ws(tmp_path / "ws")
    ws.root.mkdir()
    state = RunState(
        run_id="r",
        workspace=str(ws.root),
        spec_source=None,
        sut_source=None,
        operator_context_images=["operator-context/images/gone.png"],
    )

    abs_paths = _materialize_context_images(_opts(None), ws, state, _Log())

    assert abs_paths == []
    assert state.operator_context_images == []
