"""Vision question-answering for the ``ask_image`` tool.

Builds multimodal chat-completions requests — a text block followed by one
``image_url`` block per image (base64 data URL or http(s) URL) — and sends
them to the provider configured under config.json's ``vision_model`` key
(``"<provider>/<model-name>"``; a bare model name means the currently
selected provider).

The module is standalone on purpose: it imports nothing from the rest of
miniagent so ``tools.py`` can use it without import cycles.  Configuration
is duck-typed (anything exposing ``providers`` and optionally
``vision_model``); the API key is supplied through a callable so the
keychain scheme stays owned by ``app.py``.

Image sources understood by :meth:`Vision.ask`:

- file paths (absolute or ~-expanded) — read and, when oversized,
  EXIF-rotated / shrunk / re-encoded as JPEG until the base64 data URL
  fits the size cap,
- http(s) URLs — passed to the API as-is,
- photo-library images (Pythonista ``photos`` module, negative index =
  from the end),
- the clipboard image (Pythonista ``clipboard`` module).

Nothing here writes files, executes code, or prompts interactively.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os

import requests

# Guard rails for the request payload.  The cap applies to one image's
# base64 data URL; images above it are re-encoded until they fit.
DEFAULT_MAX_MB = 2.0
DEFAULT_LONG_SIDE = 1280
DEFAULT_MAX_TOKENS = 1024

_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heic",
}


class VisionError(Exception):
    """Raised when a vision request cannot be prepared or completed."""


def split_target(spec: str) -> tuple[str | None, str]:
    """Split a ``vision_model`` value into ``(provider, model)``.

    ``"mistral/pixtral-12b-2409"`` -> ``("mistral", "pixtral-12b-2409")``;
    a bare ``"pixtral-12b-2409"`` -> ``(None, "pixtral-12b-2409")`` (use
    the currently selected provider).
    """
    text = str(spec or "").strip()
    if "/" in text:
        provider, _, model = text.partition("/")
        return provider.strip() or None, model.strip()
    return None, text


# ---------------------------------------------------------------------------
# Image loading and encoding
# ---------------------------------------------------------------------------

def _b64_len(n_bytes: int) -> int:
    return (4 * n_bytes + 2) // 3


def _fmt_size(n_bytes: int) -> str:
    if n_bytes >= 1024 * 1024:
        return "%.1f MiB" % (n_bytes / (1024.0 * 1024.0))
    if n_bytes >= 1024:
        return "%.1f KiB" % (n_bytes / 1024.0)
    return "%d B" % n_bytes


def _mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    guess = mimetypes.guess_type(path)[0]
    return guess if guess and guess.startswith("image/") else "image/jpeg"


def data_url(mime: str, raw: bytes) -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def load_image_file(path: str) -> tuple[bytes, str]:
    """Read an image file, returning ``(raw bytes, mime)``."""
    full = os.path.expanduser(str(path).strip())
    if not os.path.isfile(full):
        raise VisionError(f"image file not found: {path}")
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise VisionError(f"cannot read {full}: {exc}") from exc
    if not raw:
        raise VisionError(f"image file is empty: {full}")
    return raw, _mime_for(full)


def _to_rgb(img):
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _encode_under_cap(img, side: int, cap_bytes: int) -> tuple[bytes, tuple[int, int]]:
    """JPEG-encode *img*, shrinking size/quality until the data URL fits."""
    from PIL import Image

    quality = 90
    attempt_side = side
    last: tuple[bytes, tuple[int, int]] = (b"", (0, 0))
    for _ in range(12):
        w, h = img.size
        if max(w, h) > attempt_side:
            scale = attempt_side / float(max(w, h))
            out = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
            )
        else:
            out = img
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=quality)
        raw = buf.getvalue()
        last = (raw, out.size)
        if _b64_len(len(raw)) + 32 <= cap_bytes:
            return last
        if quality > 60:
            quality -= 15
        elif attempt_side > 512:
            attempt_side = max(512, attempt_side // 2)
        else:
            quality = max(35, quality - 10)
            if quality == 35:
                break
    return last


def _encode_pil_image(img, cap_bytes: int, long_side: int) -> tuple[bytes, tuple[int, int]]:
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img)
    img.load()
    img = _to_rgb(img)
    side = long_side or max(img.size)
    return _encode_under_cap(img, side, cap_bytes)


def fit_file_bytes(
    raw: bytes, mime: str, cap_bytes: int, long_side: int
) -> tuple[bytes, str, tuple[int, int] | None, bool]:
    """Return ``(raw, mime, dims, shrunk)`` for a file-based image.

    Images whose data URL already fits the cap pass through untouched;
    larger ones are decoded (EXIF-rotated), shrunk and re-encoded as JPEG.
    """
    if _b64_len(len(raw)) + 32 <= cap_bytes:
        return raw, mime, None, False
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw)) as opened:
            img = ImageOps.exif_transpose(opened)
            img.load()
            img = _to_rgb(img)
        side = long_side or max(img.size)
        jpeg, dims = _encode_under_cap(img, side, cap_bytes)
        return jpeg, "image/jpeg", dims, True
    except ImportError:
        raise VisionError(
            "image is %s, over the %.1f MiB data-URL cap, and PIL is "
            "unavailable to shrink it"
            % (_fmt_size(len(raw)), cap_bytes / (1024.0 * 1024.0))
        ) from None
    except Exception as exc:
        raise VisionError(f"could not decode/resize image ({mime}): {exc}") from exc


def load_photo(idx: int, cap_bytes: int, long_side: int) -> tuple[bytes, tuple[int, int]]:
    """Load photo-library image *idx* (negative = from the end)."""
    try:
        import photos
    except Exception as exc:
        raise VisionError(f"photos module unavailable: {exc}") from exc
    try:
        count = photos.get_count()
    except Exception as exc:
        raise VisionError(
            f"cannot access the photo library ({exc}); check Pythonista's "
            "Photos permission in iOS Settings > Privacy & Security"
        ) from exc
    if count == 0:
        raise VisionError("the photo library is empty")
    if not -count <= idx < count:
        raise VisionError(f"photo index {idx} out of range (library has {count} images)")
    try:
        img = photos.get_image(idx, original=True)
    except Exception as exc:
        raise VisionError(f"could not load photo {idx}: {exc}") from exc
    if img is None:
        raise VisionError(f"photo {idx} could not be decoded (is it a video asset?)")
    return _encode_pil_image(img, cap_bytes, long_side)


def load_clipboard(cap_bytes: int, long_side: int) -> tuple[bytes, tuple[int, int]]:
    """Load the image currently on the clipboard."""
    try:
        import clipboard
    except Exception as exc:
        raise VisionError(f"clipboard module unavailable: {exc}") from exc
    try:
        img = clipboard.get_image()
    except Exception as exc:
        raise VisionError(f"could not read clipboard image: {exc}") from exc
    if img is None:
        raise VisionError("no image found on the clipboard")
    return _encode_pil_image(img, cap_bytes, long_side)


# ---------------------------------------------------------------------------
# Request building and response extraction
# ---------------------------------------------------------------------------

def build_payload(
    question: str,
    data_urls,
    model: str,
    system: str | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Build the multimodal chat-completions request body."""
    content: list[dict] = [{"type": "text", "text": question}]
    for url in data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    body = {"model": model, "messages": messages}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    return body


def extract_answer(response: dict) -> str:
    """Pull the answer text out of a chat response (string or block list)."""
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError(
            "unexpected response shape: "
            + json.dumps(response, ensure_ascii=False, default=str)[:1500]
        ) from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------------------
# The collaborator
# ---------------------------------------------------------------------------

class Vision:
    """Sends image questions to the configured vision model.

    ``load_key`` is a callable ``provider_name -> API key`` (app.py wires it
    to the Pythonista keychain scheme), so this module never touches the
    keychain itself.
    """

    def __init__(self, config, load_key, max_mb: float = DEFAULT_MAX_MB,
                 long_side: int = DEFAULT_LONG_SIDE):
        self._config = config
        self._load_key = load_key
        self._cap_bytes = int(max_mb * 1024 * 1024)
        self._long_side = long_side

    # -- target resolution -------------------------

    def _resolve_target(self) -> tuple[str, str, dict]:
        """Return ``(provider name, model, provider settings)`` for vision."""
        spec = str(getattr(self._config, "vision_model", "") or "").strip()
        if not spec:
            raise VisionError(
                'no vision_model configured: set "vision_model": '
                '"<provider>/<model-name>" in config.json (e.g. '
                '"mistral/mistral-large-latest" or '
                '"mistral/pixtral-12b-2409"), or run '
                ":config set vision_model <provider>/<model-name>"
            )
        provider, model = split_target(spec)
        providers = getattr(self._config, "providers", {}) or {}
        name = provider or str(getattr(self._config, "provider", "") or "")
        if not name or name not in providers:
            known = ", ".join(sorted(providers)) or "(none)"
            raise VisionError(
                f"vision provider {name!r} is not configured (known providers: {known})"
            )
        if not model:
            raise VisionError("vision_model is missing the model-name part")
        settings = providers[name] or {}
        if not settings.get("base_url"):
            raise VisionError(f"provider {name!r} has no base_url configured")
        return name, model, settings

    def target_label(self) -> str:
        """Short description of the configured target, for previews."""
        try:
            name, model, settings = self._resolve_target()
        except VisionError as exc:
            return f"(unavailable: {exc})"
        return f"{name}/{model} via {self._url(settings)}"

    # -- request plumbing --------------------------

    def _url(self, settings: dict) -> str:
        base = str(settings.get("base_url", "")).rstrip("/")
        path = str(settings.get("chat_path", "chat/completions")).lstrip("/")
        return f"{base}/{path}"

    def _headers(self, settings: dict, api_key: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if settings.get("auth_enabled", True) and api_key:
            prefix = settings.get("auth_prefix", "Bearer ") or ""
            name = settings.get("auth_header", "Authorization") or "Authorization"
            if prefix:
                headers[name] = f"{prefix}{api_key}"
            else:
                headers[name] = api_key
        headers.update(settings.get("extra_headers") or {})
        return headers

    def _send(self, url: str, headers: dict, payload: dict, timeout: int) -> dict:
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise VisionError(f"vision request failed: {exc}") from exc
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:1500].strip()
            raise VisionError(f"HTTP {resp.status_code} from {url}:\n{snippet}")
        try:
            return resp.json()
        except ValueError as exc:
            raise VisionError(
                f"invalid JSON response: {exc}: {(resp.text or '')[:500]}"
            ) from exc

    # -- public ------------------------------------

    def ask(self, question: str, file_paths=(), urls=(), photo=None,
            clipboard: bool = False, system: str | None = None,
            max_tokens: int | None = None) -> dict:
        """Ask *question* about the given image sources; return a result dict.

        File paths are read as-is (absolute or ~-expanded); callers that
        want workspace confinement resolve them first.  ``photo`` is a
        photo-library index (negative = from the end).  The result carries
        ``answer``, ``provider``, ``model``, ``images`` (labels) and
        ``usage``.
        """
        question = str(question or "").strip()
        if not question:
            raise VisionError("a non-empty question is required")

        name, model, settings = self._resolve_target()

        data_urls: list[str] = []
        labels: list[str] = []

        for path in file_paths:
            raw, mime = load_image_file(path)
            raw, mime, dims, shrunk = fit_file_bytes(
                raw, mime, self._cap_bytes, self._long_side
            )
            data_urls.append(data_url(mime, raw))
            dimtxt = f"{dims[0]}x{dims[1]} " if dims else ""
            note = " (auto-shrunk)" if shrunk else ""
            labels.append(f"file:{path} {dimtxt}{mime} {_fmt_size(len(raw))}{note}")

        for url in urls:
            if not str(url).lower().startswith(("http://", "https://")):
                raise VisionError(f"not an http(s) image URL: {url}")
            data_urls.append(str(url))
            labels.append(f"url:{url}")

        if photo is not None:
            if isinstance(photo, bool) or not isinstance(photo, int):
                raise VisionError("'photo' must be an integer index")
            raw, dims = load_photo(photo, self._cap_bytes, self._long_side)
            data_urls.append(data_url("image/jpeg", raw))
            labels.append(
                f"photo:{photo} {dims[0]}x{dims[1]} jpeg {_fmt_size(len(raw))}"
            )

        if clipboard:
            raw, dims = load_clipboard(self._cap_bytes, self._long_side)
            data_urls.append(data_url("image/jpeg", raw))
            labels.append(
                f"clipboard {dims[0]}x{dims[1]} jpeg {_fmt_size(len(raw))}"
            )

        if not data_urls:
            raise VisionError(
                "no image sources given (file_paths/urls/photo/clipboard)"
            )

        api_key = ""
        if settings.get("auth_enabled", True):
            api_key = str(self._load_key(name) or "").strip()
            if not api_key:
                raise VisionError(
                    f"no API key available for provider {name!r} "
                    "(store it with :key or the keychain)"
                )

        body = build_payload(
            question, data_urls, model, system,
            max_tokens if max_tokens else DEFAULT_MAX_TOKENS,
        )
        body.update(settings.get("extra_body") or {})

        timeout = settings.get("timeout", 120)
        response = self._send(self._url(settings), self._headers(settings, api_key),
                              body, int(timeout or 120))

        return {
            "ok": True,
            "answer": extract_answer(response),
            "provider": name,
            "model": response.get("model") or model,
            "images": labels,
            "usage": response.get("usage") or {},
        }
