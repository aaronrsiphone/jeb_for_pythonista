"""``ask_image``: ask the configured vision model about image(s) (gated, ASK_IMAGE).

Gated even though it is read-only because it uploads image data — possibly
photos or clipboard images from outside the workspace — to the vision
provider's API.
"""

from .. import permissions as _perm
from ..vision import VisionError
from .registry import tool


@tool(capability=_perm.ASK_IMAGE, params={
    "question": "What to ask about the image(s).",
    "images": "Image file paths inside the project workspace, or http(s) image URLs.",
    "photo": "Photo-library image index; negative counts from the end (-1 = most recent photo).",
    "clipboard": "Use the image currently on the clipboard.",
    "system": "Optional system prompt for the vision request.",
    "max_tokens": "Maximum answer tokens (default 1024).",
})
def ask_image(ctx, question: str, images: list = None, photo: int = None,
              clipboard: bool = False, system: str = None, max_tokens: int = None) -> dict:
    """Ask the configured vision model a question about one or more images.
    Image sources, in the order sent: 'images' (workspace-relative file
    paths and/or http(s) image URLs), 'photo' (iOS photo-library image
    index; negative counts from the end, so -1 is the most recent photo),
    and/or 'clipboard' (the image currently on the clipboard). The image
    data is uploaded to the vision provider's API (config.json
    'vision_model', format '<provider>/<model-name>'). Oversized images are
    automatically shrunk and re-encoded as JPEG. Returns the model's answer
    plus token usage."""
    if not isinstance(question, str) or not question.strip():
        raise VisionError("ask_image requires a non-empty 'question'")

    images = images or []
    if not isinstance(images, list) or not all(
        isinstance(p, str) and p.strip() for p in images
    ):
        raise VisionError("'images' must be a list of file paths or URLs")

    if photo is not None and (isinstance(photo, bool) or not isinstance(photo, int)):
        raise VisionError("'photo' must be an integer photo-library index")

    clipboard = bool(clipboard)
    if not images and photo is None and not clipboard:
        raise VisionError(
            "ask_image needs at least one image source: 'images' "
            "(workspace paths or URLs), 'photo' or 'clipboard'"
        )

    if ctx._vision is None:
        raise VisionError(
            "vision is not configured in this session (no Vision "
            "collaborator was passed to Tools)"
        )

    file_paths: list = []
    urls: list = []
    for item in images:
        item = item.strip()
        if item.lower().startswith(("http://", "https://")):
            urls.append(item)
        else:
            # Workspace confinement happens here: resolve() rejects paths
            # that escape the project root.
            file_paths.append(str(ctx.workspace.resolve(item)))

    return ctx._vision.ask(
        question.strip(),
        file_paths=file_paths,
        urls=urls,
        photo=photo,
        clipboard=clipboard,
        system=system,
        max_tokens=max_tokens,
    )


@ask_image.preview
def _(ctx, question: str = "", images: list = None, photo: int = None,
      clipboard: bool = False, **_kw):
    images = images or []
    lines = [f"Question: {question}"]
    for image in images:
        lines.append(f"Image: {image}")
    if photo is not None:
        lines.append(f"Photo library index: {photo} (negative = from the end)")
    if clipboard:
        lines.append("Clipboard image: yes")
    if ctx._vision is not None:
        target = ctx._vision.target_label()
    else:
        target = "(vision not configured)"
    lines.append(f"Vision target: {target}")
    lines.append("")
    lines.append("The image data will be uploaded to the vision provider's API.")
    return "ASK IMAGE", "\n".join(lines)
