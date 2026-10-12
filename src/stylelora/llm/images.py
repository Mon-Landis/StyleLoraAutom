"""JPEG copies are encoded only in memory, always from the original input."""
import hashlib
import io
import math
from pathlib import Path
import warnings

from PIL import Image, ImageOps, __version__ as PILLOW_VERSION

from .types import DEFAULT_TIERS, EncodedImage, ImageInput, ImageTier, LlmFailure


def encode_image(source: ImageInput, tier: ImageTier = ImageTier()) -> EncodedImage:
    if (source.path is None) == (source.data is None):
        raise LlmFailure('llm:image_invalid', outcome='not_sent')
    if tier.max_pixels <= 0 or tier.max_pixels > 2_000_000 or not 1 <= tier.quality <= 95:
        raise LlmFailure('llm:image_tier_invalid', outcome='not_sent')
    try:
        original = source.data if source.data is not None else Path(source.path).read_bytes()
        with warnings.catch_warnings():
            # Apply the explicit 100MP limit below rather than Pillow's warning threshold.
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(original)) as opened:
                if opened.width * opened.height > 100_000_000:
                    raise LlmFailure('llm:image_too_large', outcome='not_sent')
                opened.seek(0)
                image = ImageOps.exif_transpose(opened).convert('RGBA')
                scale = min(1, math.sqrt(tier.max_pixels / (image.width * image.height)))
                width = min(tier.max_pixels, max(1, int(image.width * scale)))
                height = min(tier.max_pixels // width, max(1, int(image.height * scale)))
                size = (width, height)
                image = image.resize(size, Image.Resampling.LANCZOS)
                background = Image.new('RGB', size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                output = io.BytesIO()
                background.save(output, 'JPEG', quality=tier.quality)
        return EncodedImage(output.getvalue(), *size, hashlib.sha256(original).hexdigest(),
                            tier, encoder_version='pillow-' + PILLOW_VERSION)
    except Image.DecompressionBombError:
        raise LlmFailure('llm:image_too_large', outcome='not_sent') from None
    except (OSError, ValueError):
        raise LlmFailure('llm:image_invalid', outcome='not_sent') from None


def encode_for_vision(path: Path, *, file_size: int, tier: ImageTier = ImageTier()) -> EncodedImage:
    # All sizes, including >=10,000,000 bytes, take the same copy/scale path.
    return encode_image(ImageInput(path=path), tier)


def next_timeout_tier(current: ImageTier, cause: str) -> ImageTier | None:
    if cause != 'timeout' or current not in DEFAULT_TIERS:
        return None
    index = DEFAULT_TIERS.index(current) + 1
    return DEFAULT_TIERS[index] if index < len(DEFAULT_TIERS) else None
