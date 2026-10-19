"""icon_extractor.py — exe 图标提取 (P1)

优先用 pywin32 提取 exe 内嵌图标并保存为 png；任何失败回退到 None，
由调用方使用默认图标。提取应在独立线程中异步调用，避免阻塞 UI。
"""
from __future__ import annotations

import os
from typing import Optional


def _clean_background(img):
    """将接近纯黑且不透明的像素设为透明，避免黑方块背景。"""
    from PIL import Image
    data = list(img.getdata())
    new = []
    for r, g, b, a in data:
        if a > 0 and r < 8 and g < 8 and b < 8:
            new.append((r, g, b, 0))
        else:
            new.append((r, g, b, a))
    img.putdata(new)
    return img


def _extract_with_win32(exe_path: str, out_path: str, size: int = 256) -> bool:
    """用 win32 API 从 exe 提取图标并保存为 png。

    使用 ctypes 直接调用 user32.DrawIconEx（绕过 pywin32 PyCDC 缺少
    DrawIconEx 的兼容性问题）。
    """
    try:
        import ctypes
        import win32gui
        import win32con
    except ImportError:
        return False

    hicon = None
    hdc = None
    hdc_mem = None
    hbmp = None
    h_old = None
    try:
        large, small = win32gui.ExtractIconEx(exe_path, 0)
        if not large and not small:
            return False
        hicon = (large or small)[0]

        # 用 ctypes 调用 GDI 绘制图标到位图（避免 PyCDC.DrawIconEx 兼容性问题）
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hdc = user32.GetDC(0)
        hbmp = gdi32.CreateCompatibleBitmap(hdc, size, size)
        hdc_mem = gdi32.CreateCompatibleDC(hdc)
        h_old = gdi32.SelectObject(hdc_mem, hbmp)

        # DI_NORMAL = 3：绘制完整图标（含背景）
        user32.DrawIconEx(hdc_mem, 0, 0, hicon, size, size, 0, None, 3)

        # 取位图数据
        bmpinfo = gdi32.GetDIBits(
            hdc_mem, hbmp, 0, size, None,
            ctypes.byref(ctypes.create_string_buffer(40)), 0, 0,
        )
        # 用更可靠的方式：GetBitmapBits + PIL frombuffer
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int),
                ("biHeight", ctypes.c_int),
                ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = size
        bmi.biHeight = -size  # 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32  # BGRA

        buf = ctypes.create_string_buffer(size * size * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, size, buf, ctypes.byref(bmi), 0)

        from PIL import Image
        img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)
        img = _clean_background(img)
        img.save(out_path)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[icon] 提取失败 {exe_path}: {e}")
        return False
    finally:
        try:
            if h_old and hdc_mem:
                ctypes.windll.gdi32.SelectObject(hdc_mem, h_old)
            if hbmp:
                ctypes.windll.gdi32.DeleteObject(hbmp)
            if hdc_mem:
                ctypes.windll.gdi32.DeleteDC(hdc_mem)
            if hdc:
                ctypes.windll.user32.ReleaseDC(0, hdc)
            if hicon:
                win32gui.DestroyIcon(hicon)
        except Exception:
            pass


def extract_icon(exe_path: str, output_dir: str, game_id: int) -> Optional[str]:
    """从 exe 提取图标存为 {game_id}.png；失败返回 None。"""
    if not os.path.isfile(exe_path):
        return None
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{game_id}.png")
    if _extract_with_win32(exe_path, out_path):
        return out_path
    return None


def ensure_default_icon(path: str, size: int = 256) -> str:
    """若默认占位图标不存在则用 PIL 生成一个（深色圆角 + 播放三角）。"""
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (30, 38, 56, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.12),
                        fill=(46, 58, 84, 255))
    cx, cy = size / 2, size / 2
    s = size * 0.22
    d.polygon([(cx - s, cy - s), (cx - s, cy + s), (cx + s * 1.3, cy)],
              fill=(120, 160, 220, 255))
    img.save(path)
    return path


if __name__ == "__main__":
    import tempfile
    p = ensure_default_icon(os.path.join(tempfile.gettempdir(), "ag_default_icon.png"))
    print("default icon:", p)
