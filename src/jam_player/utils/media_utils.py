from PIL import Image as PILImage
import os
import typing as tp
import requests
from jam_player.jam_enums import SceneMediaType
import time
from requests.exceptions import ReadTimeout, ConnectionError


def fix_dimensions_if_too_big(img_path: str, max_dimension: int = 4096):
    """
    Checks if the image dimensions exceed the specified maximum (default 4096).
    If so, resizes the image while maintaining the aspect ratio.

    :param img_path: Path to the image file
    :param max_dimension: Maximum allowed dimension (default 4096)
    """
    print(f"Checking and fixing dimensions of image at {img_path}")

    with PILImage.open(img_path) as img:
        width, height = img.size

        if width > max_dimension or height > max_dimension:
            print(f"Image dimensions ({width}x{height}) exceed {max_dimension}. Resizing...")

            # Calculate the scaling factor
            scale = max_dimension / max(width, height)

            # Calculate new dimensions
            new_width = int(width * scale)
            new_height = int(height * scale)

            # Resize the image
            resized_img = img.resize((new_width, new_height), PILImage.LANCZOS)

            # Save the resized image back to the same file
            resized_img.save(img_path, quality=95, optimize=True)

            print(f"Image resized to {new_width}x{new_height}")
        else:
            print(f"Image dimensions ({width}x{height}) are within the limit. No resizing needed.")


def fix_image_orientation(img: PILImage, img_path: str):
    exif = img.getexif()

    orientation_key = 274  # This is the key for their 'Orientation' enum
    # print(f"{img_path} -------- Orientation key: {orientation_key}")
    if orientation_key is not None:
        orientation = exif.get(orientation_key)
        # print(f"{img_path} -------- Orientation before fix: {orientation}")

        if orientation == 3:
            img = img.rotate(180, expand=True)
            img.save(img_path)
        elif orientation == 6:
            img = img.rotate(270, expand=True)
            img.save(img_path)
        elif orientation == 8:
            img = img.rotate(90, expand=True)
            img.save(img_path)


def compress_and_save_image(img: PILImage, image_path, target_size_mb=2.75):
    try:
        # Find the compression ratio needed
        ratio = (target_size_mb * 1024 * 1024) / os.path.getsize(image_path)
        # Calculate new size
        new_size = tuple(int(dim * ratio**0.5) for dim in img.size)

        # Resize the image
        img = img.resize(new_size, PILImage.Resampling.LANCZOS)

        # Save the image back to the same path
        if img.format == 'JPEG':
            img.save(image_path, quality=85, optimize=True)
        elif img.format == 'PNG':
            img.save(image_path, compress_level=9, optimize=True)
        elif img.format == 'WEBP':
            # WEBP can use either lossy or lossless compression
            img.save(image_path, quality=80, optimize=True, lossless=False)
        else:
            # For other formats, just save without specific compression
            img.save(image_path)
    except Exception as e:
        print(f"Error compressing image: {e}. Image will remain original size.")


def fix_image(
        img_path: str,
        max_allowed_size_mb: float = 3,
        target_compressed_size_mb: float = 2.5
):
    print(
        f"Fixing image at {img_path}. "
        f"Max allowed size in MB: {max_allowed_size_mb}. "
        f"Target compressed size in MB: {target_compressed_size_mb}"
    )

    fix_dimensions_if_too_big(img_path)

    with PILImage.open(img_path) as img:
        fix_image_orientation(img, img_path)

    with PILImage.open(img_path) as img:
        # Check and compress image if needed
        print(
            f"    Checking whether compression is necessary. Image size: {os.path.getsize(img_path)}. "
            f"Max allowed size: {max_allowed_size_mb * 1024 * 1024}."
        )
        if os.path.getsize(img_path) >= max_allowed_size_mb * 1024 * 1024:
            print("    Compression necessary. Compressing ...")
            compress_and_save_image(img, img_path, target_size_mb=target_compressed_size_mb)
        else:
            print("    Compression not necessary")


def get_file_extension(file_content: bytes, media_type: SceneMediaType) -> str:
    """
    Determine file extension from content using PIL for images and byte detection for videos.
    Returns appropriate file extension including the dot.
    """
    if media_type == SceneMediaType.IMAGE:
        # Use PIL for images
        import io
        try:
            with PILImage.open(io.BytesIO(file_content)) as img:
                fmt = img.format.lower()
                if fmt == 'jpeg':
                    return '.jpg'
                return f'.{fmt}'
        except Exception as e:
            print(f"Error detecting image format, defaulting to .jpg: {e}")
            return '.jpg'
    else:
        # Handle video types
        if file_content.startswith(b'\x00\x00\x00\x1c\x66\x74\x79\x70'):  # MP4
            return '.mp4'
        elif file_content.startswith(b'\x52\x49\x46\x46'):  # AVI
            return '.avi'
        elif file_content.startswith(b'\x00\x00\x00\x14\x66\x74\x79\x70'):  # MOV
            return '.mov'

        print("Could not determine video format, defaulting to .mp4")
        return '.mp4'


def download_media(
        media_url,
        download_file_path,
        media_type: SceneMediaType
) -> tp.Union[bool, str]:
    # Returns False if the image was already downloaded or there was no image to download
    print(f"Handling request to download image to {download_file_path}")
    if media_url:
        if not media_url.startswith(('http:', 'https:')):
            media_url = 'https:' + media_url

        for i in range(12):
            try:
                image_response = requests.get(media_url)
                break
            except (ConnectionError, ReadTimeout):
                if i < 11:
                    print(
                        "Caught Connector or Timeout Error while downloading an "
                        "image, trying request again ..."
                    )
                    time.sleep(5)
                else:
                    # Last try, raise the caught exception
                    raise

        # If download_file_path doesn't have an extension, determine it from content
        if not os.path.splitext(download_file_path)[1]:
            extension = get_file_extension(image_response.content, media_type)
            download_file_path = f"{download_file_path}{extension}"

        directory = os.path.dirname(download_file_path)
        if not os.path.exists(directory):
            os.makedirs(directory)

        with open(download_file_path, "wb") as f:
            f.write(image_response.content)
    else:
        print(
            "The url of the media to download is empty. "
            f"Skipping download to file {download_file_path}"
        )
        return False
    return download_file_path

