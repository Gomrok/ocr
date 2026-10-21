"""Classical, reversible candidate preparation in reference-pixel coordinates."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import warnings

import cv2
import numpy as np
import pymupdf

from .profiles import Options


class SourcePages:
    def __init__(self, path: Path, options: Options):
        self.path, self.options = path, options
        if path.stat().st_size > options.max_document_bytes:
            raise ValueError("Source size exceeds budget")
        self.pdf = path.suffix.lower() == ".pdf"
        if self.pdf:
            self.document = pymupdf.open(path)
            if self.document.needs_pass:
                self.document.close()
                raise ValueError("Encrypted source requires an unlocked copy")
            self.count = self.document.page_count
        else:
            from PIL import Image
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    self.document = Image.open(path)
            except (Image.DecompressionBombWarning, Image.DecompressionBombError):
                raise ValueError("Image header exceeds safe dimensions") from None
            if self.document.format not in ("PNG", "JPEG", "TIFF", "BMP"):
                self.document.close()
                raise ValueError("Unsupported image format")
            self.count = 1
            if self.document.format == "TIFF":
                # Do not let n_frames traverse an unbounded adversarial TIFF directory chain.
                try:
                    for index in range(1, 301):
                        try:
                            self.document.seek(index)
                            self.count = index+1
                        except EOFError:
                            break
                    self.document.seek(0)
                except Exception:
                    self.document.close()
                    raise
        if not 1 <= self.count <= 300:
            self.document.close()
            raise ValueError("Page count exceeds budget")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.document.close()

    def read(self, index: int):
        if self.pdf:
            page = self.document[index]
            width = math.ceil(page.rect.width * self.options.dpi / 72)
            height = math.ceil(page.rect.height * self.options.dpi / 72)
            self._check_dimensions(width, height)
            pix = page.get_pixmap(dpi=self.options.dpi, colorspace=pymupdf.csGRAY, alpha=False)
            image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
            metadata = {"dpi": self.options.dpi, "pdf_rect": list(page.rect),
                        "pdf_rotation": page.rotation, "embedded_text_used": False}
        else:
            from PIL import Image
            self.document.seek(index)
            self._check_dimensions(*self.document.size)
            # Alpha compositing is deterministic. EXIF orientation is reported, not silently applied.
            rgba = self.document.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = np.asarray(Image.alpha_composite(white, rgba).convert("L")).copy()
            metadata = {"dpi": None, "image_dpi_metadata": self.document.info.get("dpi"),
                        "exif_orientation": self.document.getexif().get(274, 1), "alpha_background": "white"}
        return image, metadata

    def _check_dimensions(self, width, height):
        if min(width, height) <= 0 or width * height > self.options.max_pixels:
            raise ValueError("Reference page exceeds pixel budget")


@dataclass
class Variant:
    name: str
    image: np.ndarray
    reference_to_image: np.ndarray
    steps: list[dict]

    def reference_box(self, box, reference_shape):
        x0, y0, x1, y1 = box
        points = np.array([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]], np.float64)
        result = cv2.perspectiveTransform(points, np.linalg.inv(self.reference_to_image))[0]
        h, w = reference_shape[:2]
        return [max(0, math.floor(result[:, 0].min())), max(0, math.floor(result[:, 1].min())),
                min(w, math.ceil(result[:, 0].max())), min(h, math.ceil(result[:, 1].max()))]

    def metadata(self):
        return {"family": self.name, "reference_to_image": self.reference_to_image.tolist(),
                "image_to_reference": np.linalg.inv(self.reference_to_image).tolist(), "steps": self.steps}


def rotate_expanded(image, angle):
    h, w = image.shape
    transform = cv2.getRotationMatrix2D((w/2, h/2), angle, 1)
    cos, sin = abs(transform[0, 0]), abs(transform[0, 1])
    nw, nh = math.ceil(h*sin+w*cos), math.ceil(h*cos+w*sin)
    transform[0, 2] += (nw-w)/2
    transform[1, 2] += (nh-h)/2
    out = cv2.warpAffine(image, transform, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=255)
    return out, np.vstack((transform, [0, 0, 1]))


def skew_angle(image):
    # Only straight, mutually consistent near-horizontal evidence is used.
    # Limit geometric analysis cost; recognition and coverage still use original pixels.
    scale = min(1., 1200/max(image.shape))
    analysis = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
    edges = cv2.Canny(analysis, 40, 120)
    lines = cv2.HoughLinesP(edges, 1, np.pi/360, threshold=max(30, analysis.shape[1]//8),
                            minLineLength=max(35, analysis.shape[1]//6), maxLineGap=12)
    if lines is None:
        return None
    angles = [math.degrees(math.atan2(y1-y0, x1-x0)) for x0, y0, x1, y1 in lines.reshape(-1, 4)
              if x1 != x0 and abs(math.degrees(math.atan2(y1-y0, x1-x0))) < 7]
    if len(angles) < 6:
        return None
    median = float(np.median(angles))
    if np.median(np.abs(np.array(angles)-median)) > .6 or abs(median) < .2:
        return None
    return median


def variants(reference, box, options: Options, *, full_page=False, remove_rules=False):
    """Base pixels are never mutated; morphology is used only on a named candidate."""
    x0, y0, x1, y1 = box
    crop = reference[y0:y1, x0:x1]
    padding = 12
    base = cv2.copyMakeBorder(crop, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=255)
    transform = np.array([[1., 0, padding-x0], [0, 1., padding-y0], [0, 0, 1.]])
    yield Variant("original", base, transform, [{"operation": "crop_with_margin", "bbox": list(box), "padding": padding}])
    image, matrix = base.copy(), transform.copy()
    steps = []
    if full_page and options.orientation_degrees:
        image, rotation = rotate_expanded(image, options.orientation_degrees)
        matrix = rotation @ matrix
        steps.append({"operation": "declared_orientation", "degrees": options.orientation_degrees})
    if full_page and options.perspective_corners:
        points = np.array(options.perspective_corners, np.float32)
        h, w = reference.shape
        if (not np.isfinite(points).all() or np.any(points < 0) or np.any(points[:, 0] >= w)
                or np.any(points[:, 1] >= h) or not cv2.isContourConvex(points)
                or abs(cv2.contourArea(points)) < w*h*.25):
            raise ValueError("Perspective corners are not a valid document quadrilateral")
        target = np.float32([[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]])
        source_points = cv2.perspectiveTransform(points[None], matrix)[0]
        perspective = cv2.getPerspectiveTransform(source_points, target)
        image = cv2.warpPerspective(image, perspective, (w, h), borderValue=255)
        matrix = perspective @ matrix
        steps.append({"operation": "declared_perspective", "corners": points.tolist()})
    angle = skew_angle(image) if options.deskew else None
    if angle is not None:
        image, rotation = rotate_expanded(image, angle)
        matrix = rotation @ matrix
        steps.append({"operation": "deskew_candidate", "degrees": angle})
    yield Variant("geometry", image.copy(), matrix.copy(), steps.copy())
    if options.trim_black_border:
        # Only black runs connected to the four exterior edges of the crop.
        for view in (image, image[::-1], image.T, image.T[::-1]):
            for i in range(min(20, len(view)//50)):
                if (view[i] < 35).mean() < .98:
                    break
                view[i] = 255
        steps.append({"operation": "black_border_candidate"})
    if options.denoise:
        image = cv2.bilateralFilter(image, 3, 8, 8)
        steps.append({"operation": "mild_bilateral_candidate"})
    if options.remove_shadow:
        background = cv2.GaussianBlur(image, (0, 0), 35)
        image = cv2.divide(image, np.maximum(background, 1), scale=255)
        steps.append({"operation": "shadow_candidate"})
    if options.contrast:
        image = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(8, 8)).apply(image)
        steps.append({"operation": "mild_clahe_candidate", "clip_limit": 1.4})
    yield Variant("gray", image.copy(), matrix.copy(), steps.copy())
    if options.adaptive:
        thresholded = cv2.adaptiveThreshold(image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY, 41, 13)
        yield Variant("adaptive", thresholded, matrix.copy(), steps + [{"operation": "adaptive_candidate"}])
    if remove_rules:
        from ..tables import strip_grid_lines
        yield Variant("without_rules", strip_grid_lines(base), transform, [{"operation": "rules_candidate_only"}])


def blank_evidence(image):
    # Deliberately strict: an empty OCR result or a faint image is not blank evidence.
    return {"verified_uniform_white": bool(np.all(image == 255)),
            "min_intensity": int(image.min()), "max_intensity": int(image.max()),
            "dark_pixel_fraction": float((image < 245).mean())}


def uncovered_regions(image, words, *, tile_size=900):
    """Overlapping full-resolution analysis, including edges/footnotes; never erases the source."""
    h, w = image.shape
    result = []
    step = tile_size - 100
    for y in range(0, h, step):
        for x in range(0, w, step):
            x1, y1 = min(w, x+tile_size), min(h, y+tile_size)
            crop = image[y:y1, x:x1]
            background = cv2.GaussianBlur(crop, (0, 0), 9)
            ink = ((background.astype(np.int16)-crop.astype(np.int16)) > 6).astype(np.uint8)
            for word in words:
                a, b, c, d = word["bbox"]
                if a < x1 and c > x and b < y1 and d > y:
                    ink[max(0,b-y-3):min(y1-y,d-y+3), max(0,a-x-3):min(x1-x,c-x+3)] = 0
            # A coverage signal, not a claim that these pixels are definitely text.
            if np.count_nonzero(ink) >= 30:
                points = cv2.findNonZero(ink)
                a, b, c, d = cv2.boundingRect(points)
                result.append([max(0,x+a-16), max(0,y+b-16), min(w,x+a+c+16), min(h,y+b+d+16)])
    return result
