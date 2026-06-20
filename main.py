import ctypes
import math
import time

# 先启用 Windows DPI awareness，尽量避免截图像素坐标与鼠标坐标错位
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import cv2
import mss
import numpy as np
import pyautogui


# ============================================================
# 配置
# ============================================================

# PrintScreen 原图是 2048 x 1152 则半径约为 74 px。
RADIUS = 74

# 为避免边缘抗锯齿、游戏判定半径略小于视觉半径等问题，
# 可以把实际用于优化的半径稍微调小。
# 若发现实际消除范围比画面中的虚线圈更小，可改成 70 或 71。
EFFECTIVE_RADIUS = 74

# 只在中间主要生成区域中搜索圆心。
SEARCH_X_MIN_RATIO = 0.16
SEARCH_X_MAX_RATIO = 0.85

# 上下也稍微留边，防止圆心被放到画面边缘。
SEARCH_Y_MIN_RATIO = 0.03
SEARCH_Y_MAX_RATIO = 0.98

# False 时只计算、打印坐标、输出调试图，不移动鼠标。
# 确认正确后改为 True。
MOVE_MOUSE = True

# 是否保存带有最佳圆位置的调试图。
SAVE_DEBUG_IMAGE = False
DEBUG_IMAGE_NAME = "aim_debug.png"

# 如果需要连续跟踪，就改为 True。
# 连续模式每次截图、计算、移动，然后等待 INTERVAL 秒。
LOOP = True
INTERVAL = 0.5

# 游戏在主显示器时通常是 1。
# 若游戏在第二块显示器，可改成 2。
MONITOR_INDEX = 1

# pyautogui 移动速度，0 表示立即移动。
MOVE_DURATION = 0.0

# 低于此覆盖率时不移动，避免画面中几乎没有目标时乱移。
MIN_RED_COVERAGE = 0.03


# ============================================================
# 工具函数
# ============================================================

def make_disc_kernel(radius: int) -> np.ndarray:
    """生成半径为 radius 的圆盘核。"""
    y, x = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    disc = (x * x + y * y <= radius * radius).astype(np.float32)
    return disc


DISC_KERNEL = make_disc_kernel(EFFECTIVE_RADIUS)
DISC_AREA = float(DISC_KERNEL.sum())


def extract_red_mask(frame_bgr: np.ndarray) -> np.ndarray:
    """
    提取砖红色目标区域。

    这张图中的砖红色大致接近：
    RGB ≈ (152, 89, 93)
    OpenCV HSV hue 接近 178-179。

    使用较宽的红色阈值，以兼容抗锯齿、透明度和轻微亮度变化。
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)

    # 接近 OpenCV hue = 180 一侧的砖红色
    lower_red_1 = np.array([165, 55, 75], dtype=np.uint8)
    upper_red_1 = np.array([180, 180, 210], dtype=np.uint8)

    # 兼容 hue 环绕到 0 附近的红色
    lower_red_2 = np.array([0, 55, 75], dtype=np.uint8)
    upper_red_2 = np.array([5, 180, 210], dtype=np.uint8)

    mask_1 = cv2.inRange(hsv, lower_red_1, upper_red_1)
    mask_2 = cv2.inRange(hsv, lower_red_2, upper_red_2)

    mask = cv2.bitwise_or(mask_1, mask_2)

    # 很轻的闭运算，补一下多边形边缘因抗锯齿产生的小断点。
    # 不做大的膨胀，避免把相邻目标过度合并。
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    return (mask > 0).astype(np.float32)


def find_best_center(frame_bgr: np.ndarray):
    """
    对砖红色二值图做圆盘卷积。

    score[y, x] 表示：
    以 (x, y) 为圆心、半径 EFFECTIVE_RADIUS 的圆中，
    一共覆盖多少砖红色像素。

    因为圆面积固定，最大化红色像素数等价于最小化空白面积。
    """
    height, width = frame_bgr.shape[:2]

    red_mask = extract_red_mask(frame_bgr)

    # 每个位置的值 = 固定半径圆内红色像素总数
    score = cv2.filter2D(
        red_mask,
        ddepth=-1,
        kernel=DISC_KERNEL,
        borderType=cv2.BORDER_CONSTANT,
    )

    # 限制可选圆心区域，排除左右不生成多边形的空白区域。
    x0 = int(width * SEARCH_X_MIN_RATIO)
    x1 = int(width * SEARCH_X_MAX_RATIO)
    y0 = int(height * SEARCH_Y_MIN_RATIO)
    y1 = int(height * SEARCH_Y_MAX_RATIO)

    valid = np.zeros((height, width), dtype=bool)
    valid[y0:y1, x0:x1] = True

    # 圆心离边界太近时，圆会超出画面。
    # 这里再缩一圈，避免在边缘出现卷积边界偏差。
    margin = EFFECTIVE_RADIUS
    valid[:margin, :] = False
    valid[-margin:, :] = False
    valid[:, :margin] = False
    valid[:, -margin:] = False

    score[~valid] = -np.inf

    best_flat_index = int(np.argmax(score))
    best_y, best_x = np.unravel_index(best_flat_index, score.shape)

    best_score = float(score[best_y, best_x])
    red_coverage = best_score / DISC_AREA

    return {
        "x": int(best_x),
        "y": int(best_y),
        "score": best_score,
        "coverage": red_coverage,
        "red_mask": red_mask,
        "search_box": (x0, y0, x1, y1),
    }


def save_debug_image(frame_bgr: np.ndarray, result: dict):
    """
    输出调试图：
    蓝框为允许搜索的区域，
    绿圈为最佳鼠标圈，
    中心点为计算出的最佳鼠标位置。
    """
    debug = frame_bgr.copy()

    x0, y0, x1, y1 = result["search_box"]
    best_x = result["x"]
    best_y = result["y"]

    cv2.rectangle(debug, (x0, y0), (x1, y1), (255, 100, 0), 2)
    cv2.circle(debug, (best_x, best_y), RADIUS, (0, 220, 0), 3)
    cv2.circle(debug, (best_x, best_y), 5, (0, 220, 0), -1)

    text = (
        f"x={best_x}, y={best_y}, "
        f"red coverage={result['coverage'] * 100:.1f}%"
    )

    cv2.putText(
        debug,
        text,
        (20, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (20, 20, 20),
        4,
        cv2.LINE_AA,
    )

    cv2.putText(
        debug,
        text,
        (20, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    cv2.imwrite(DEBUG_IMAGE_NAME, debug)


def screenshot_and_move():
    """
    截图、寻找最佳位置，并在需要时移动鼠标。
    """
    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR_INDEX]

        shot = np.array(sct.grab(monitor))
        frame_bgr = shot[:, :, :3]

        result = find_best_center(frame_bgr)

        best_x = result["x"]
        best_y = result["y"]
        coverage = result["coverage"]

        print(
            f"最佳截图坐标: ({best_x}, {best_y}) | "
            f"圆内砖红色覆盖率: {coverage * 100:.2f}%"
        )

        if SAVE_DEBUG_IMAGE:
            save_debug_image(frame_bgr, result)
            print(f"已写出调试图: {DEBUG_IMAGE_NAME}")

        if coverage < MIN_RED_COVERAGE:
            print("红色覆盖率过低，不移动鼠标。")
            return result

        if MOVE_MOUSE:
            py_screen_w, py_screen_h = pyautogui.size()
            shot_h, shot_w = frame_bgr.shape[:2]

            mouse_x = monitor["left"] + round(best_x * py_screen_w / shot_w)
            mouse_y = monitor["top"] + round(best_y * py_screen_h / shot_h)

            pyautogui.moveTo(mouse_x, mouse_y, duration=MOVE_DURATION)
            print(f"鼠标已移动到: ({mouse_x}, {mouse_y})")

        return result


# ============================================================
# 运行
# ============================================================

if __name__ == "__main__":
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True

    print("程序将在 3 秒后开始。请先将游戏窗口置于前台。")
    print("将鼠标移到主屏幕左上角可正常停止程序。")
    time.sleep(3)

    try:
        if LOOP:
            while True:
                screenshot_and_move()
                time.sleep(INTERVAL)
        else:
            screenshot_and_move()

    except pyautogui.FailSafeException:
        print("\n检测到鼠标位于左上角（0,0），程序已停止。")

    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C，程序已停止。")