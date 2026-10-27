"""Read-only adapter for the supplied 16×16 ViTac JSON sensor firmware.

No actuator protocol exists in the supplied source. This module never writes to
the serial port. pyserial is deliberately imported only when hardware is used.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import importlib
import json
import math
from pathlib import Path
import threading
import time
from typing import Mapping, Sequence


POINTS = 256
UINT32_MAX = (1 << 32) - 1
MAX_LINE_BYTES = 8192
DEFAULT_MAPPING = Path(__file__).resolve().parent.parent / "config" / "zones.json"


@dataclass
class SensorFrame:
    zones: list[float] | None
    raw_zones: list[float] | None
    matrix: list[int] | None
    raw_matrix: list[int] | None
    seq: int
    device_time_ms: int
    received_at: float
    valid: bool = True


def _integer(value: object, low: int, high: int, name: str) -> int:
    # bool is an int subclass, but is not a measurement or sequence number.
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low}..{high} 的整数")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 存在重复键：{key}")
        result[key] = value
    return result


def parse_frame(line: str | bytes) -> dict | None:
    """Parse a complete line; comments/blank lines return None, errors raise.

    Required fields are seq, t, base, diff; raw and the firmware's descriptive
    statistics are optional. A base=0 payload is structurally valid, but its
    SensorFrame.valid will be false. No sensor values are inferred from stats.
    """
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("串口行过长")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("串口数据不是 UTF-8；请检查波特率") from exc
    if not isinstance(line, str):
        raise ValueError("串口帧必须是一行文本")
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("串口行过长")
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    try:
        data = json.loads(line, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("串口行不是有效 JSON 帧") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 帧必须是对象")
    for key in ("seq", "t", "base", "diff"):
        if key not in data:
            raise ValueError(f"JSON 帧缺少 {key}")
    _integer(data["seq"], 0, UINT32_MAX, "seq")
    _integer(data["t"], 0, UINT32_MAX, "t")
    _integer(data["base"], 0, 1, "base")
    for key in ("diff", "raw"):
        if key not in data:
            continue
        values = data[key]
        if not isinstance(values, list) or len(values) != POINTS:
            raise ValueError(f"{key} 必须包含 256 个数")
        for value in values:
            _integer(value, 0, 255, key)
    for key, low, high in (("peak", 0, 255), ("pr", -1, 15),
                           ("pc", -1, 15), ("nz", 0, 256),
                           ("sum", 0, 65280)):
        if key in data:
            _integer(data[key], low, high, key)
    return data


def _validate_mapping(mapping: object) -> dict[str, list[int]]:
    if not isinstance(mapping, dict) or set(mapping) != {str(n) for n in range(1, 9)}:
        raise ValueError("分区映射必须恰好包含字符串键 1..8")
    result: dict[str, list[int]] = {}
    used: set[int] = set()
    for zone in map(str, range(1, 9)):
        indices = mapping[zone]
        if not isinstance(indices, list):
            raise ValueError(f"分区 {zone} 必须是索引列表")
        result[zone] = []
        for index in indices:
            _integer(index, 0, 255, f"分区 {zone} 索引")
            if index in used:
                raise ValueError(f"矩阵索引 {index} 被重复分配")
            used.add(index)
            result[zone].append(index)
    return result


def load_mapping(path: str | Path = DEFAULT_MAPPING) -> dict[str, list[int]]:
    """Load disjoint zones. Empty/partial mappings are valid but not ready.

    All eight zones must be nonempty to enable aggregation. Unassigned cells are
    permitted: a measured zone need not use every physical pixel of the cloth.
    """
    try:
        with Path(path).open(encoding="utf-8-sig") as source:
            return _validate_mapping(json.load(source, object_pairs_hook=_unique_object))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取分区映射：{exc}") from exc


def map_zones(values: Sequence[int | float], mapping: Mapping[str, list[int]]) -> list[float] | None:
    """Mean each physical zone; an incomplete mapping returns None, never zero."""
    checked = _validate_mapping(mapping)
    if len(values) != POINTS or any(
        type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 255
        for value in values
    ):
        raise ValueError("分区聚合需要 256 个 0..255 的有限数值")
    if any(not indices for indices in checked.values()):
        return None
    return [sum(values[index] for index in checked[str(zone)]) / len(checked[str(zone)])
            for zone in range(1, 9)]


def list_serial_ports() -> list[dict]:
    try:
        ports = importlib.import_module("serial.tools.list_ports")
    except ImportError:
        return []
    return [{"device": port.device, "description": port.description or port.device}
            for port in ports.comports()]


class SerialSensor:
    """One bounded reader thread, latest-frame handoff, and host-only zeroing."""

    def __init__(self, port: str, baud: int = 2000000,
                 mapping_path: str | Path = DEFAULT_MAPPING):
        if not isinstance(port, str) or not port.strip():
            raise ValueError("请选择串口")
        if type(baud) is not int or not 1 <= baud <= 4000000:
            raise ValueError("无效波特率")
        self.port = port
        self.baud = baud
        self.mapping = load_mapping(mapping_path)
        self.mapping_ready = all(self.mapping.values())
        self.connected = False
        self.message = "串口尚未连接"
        self.sequence_status = "waiting"
        self._serial = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: SensorFrame | None = None
        self._last_seq: int | None = None
        self._last_device_time: int | None = None
        self._last_raw_at: float | None = None
        self._recent_raw: deque[tuple[float, list[int]]] = deque(maxlen=120)
        self._host_baseline: list[int] | None = None
        self._reset_required = False

    def connect(self) -> None:
        if self.connected:
            return
        self.close()
        try:
            serial = importlib.import_module("serial")
        except ImportError as exc:
            raise ValueError("真实串口需安装 pyserial：python -m pip install pyserial") from exc
        try:
            serial_port = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.1)
        except Exception as exc:
            raise ValueError(f"无法打开串口 {self.port}：{exc}") from exc
        with self._lock:
            self._serial = serial_port
            self._pending = None
            self._last_seq = None
            self._last_device_time = None
            self._last_raw_at = None
            self._recent_raw.clear()
            self._host_baseline = None
            self._reset_required = False
            self.sequence_status = "waiting"
            self.connected = True
            self.message = "已连接，等待 JSON 帧；连接串口可能令 Nano 重启，请保持布面空载"
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="vitac-read-only", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        serial_port, self._serial = self._serial, None
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            self.connected = False
            self._pending = None
            self._recent_raw.clear()
            self._last_raw_at = None
            self._host_baseline = None
            self.message = "串口已关闭"

    def read(self) -> SensorFrame | None:
        with self._lock:
            frame, self._pending = self._pending, None
            return frame

    def zero(self) -> None:
        """Average a recent stable empty-cloth raw window; no board command.

        The caller must obtain explicit empty-cloth confirmation. Require >=5
        frames spanning >=0.5 s in the latest second, fresh within 0.5 s, and
        each pixel's observed range <=20 ADC/4 units. Reconnect discards zero.
        """
        now = time.monotonic()
        with self._lock:
            if self._reset_required:
                raise ValueError("检测到板端重启，请重新连接后再归零")
            if self._last_raw_at is None or now - self._last_raw_at > 0.5:
                raise ValueError("归零需要新鲜 raw 数据；请确认 JSON 保留 raw 数组")
            frames = [(at, raw) for at, raw in self._recent_raw if now - at <= 1.0]
            if len(frames) < 5 or frames[-1][0] - frames[0][0] < 0.5:
                raise ValueError("请保持布面空载稳定至少 1 秒后再归零")
            columns = list(zip(*(raw for _, raw in frames)))
            if any(max(column) - min(column) > 20 for column in columns):
                raise ValueError("空载 raw 波动过大；请保持布面静止后重试")
            self._host_baseline = [round(sum(column) / len(column)) for column in columns]
            self._pending = None  # The next fresh frame must use the new baseline.
            self.message = "电脑端空载基线已更新；未向板端发送命令"

    def _reject(self, message: str, now: float) -> None:
        """Invalidate immediately without copying an old measurement."""
        with self._lock:
            self.message = message
            self._last_raw_at = None
            self._recent_raw.clear()
            self._pending = SensorFrame(None, None, None, None, self._last_seq or 0,
                                        self._last_device_time or 0, now, valid=False)

    def _process_line(self, line: str | bytes, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        try:
            payload = parse_frame(line)
        except ValueError as exc:
            self._reject(str(exc), now)
            return
        if payload is None:
            return
        seq, device_time = payload["seq"], payload["t"]
        with self._lock:
            if self._reset_required:
                return
            if self._last_seq is not None:
                seq_delta = (seq - self._last_seq) & UINT32_MAX
                time_delta = (device_time - self._last_device_time) & UINT32_MAX
                if seq_delta == 0:
                    self.sequence_status = "duplicate"
                    self.message = "重复帧已忽略；未刷新数据时间"
                    return
                if seq_delta >= (1 << 31) or time_delta >= (1 << 31):
                    # Without a boot id, an old early packet cannot be proven
                    # different from a reboot. Fail closed on that ambiguity.
                    if seq <= 10 and device_time <= 5000 and device_time < self._last_device_time:
                        self.sequence_status = "reset"
                        self._reset_required = True
                        self._host_baseline = None
                        self._last_raw_at = None
                        self._recent_raw.clear()
                        self._pending = SensorFrame(None, None, None, None, seq,
                                                    device_time, now, valid=False)
                        self.message = "可能检测到板端重启，已清除电脑端基线；请重新连接"
                    else:
                        self.sequence_status = "reordered"
                        self.message = "倒序帧已忽略；未刷新数据时间"
                    return
                self.sequence_status = "gap" if seq_delta > 1 else "ok"
            else:
                self.sequence_status = "first"
            self._last_seq = seq
            self._last_device_time = device_time
            raw = payload.get("raw")
            matrix = payload["diff"]
            valid = payload["base"] == 1
            if raw is not None and valid:
                self._last_raw_at = now
                self._recent_raw.append((now, raw.copy()))
            else:
                self._last_raw_at = None
                self._recent_raw.clear()
            if self._host_baseline is not None:
                if raw is None:
                    matrix = None
                    valid = False
                else:
                    matrix = [max(0, value - base) for value, base in zip(raw, self._host_baseline)]
            zones = map_zones(matrix, self.mapping) if matrix is not None else None
            raw_zones = map_zones(raw, self.mapping) if raw is not None else None
            self._pending = SensorFrame(zones, raw_zones, matrix, raw, seq,
                                        device_time, now, valid=valid)
            if payload["base"] == 0:
                self.message = "板端 base=0，未完成空载校准；数据不可用于控制"
            elif matrix is None:
                self.message = "电脑端归零后缺少 raw，数据不可用于控制"
            elif not self.mapping_ready:
                self.message = "矩阵已接收；8 个分区尚未全部配置，禁止自动控制"
            else:
                self.message = "只读接收正常；真实执行器未启用"

    def _reader(self) -> None:
        buffer = bytearray()
        discarding = False
        try:
            while not self._stop.is_set():
                serial_port = self._serial
                if serial_port is None:
                    return
                data = serial_port.read(min(max(serial_port.in_waiting, 1), 4096))
                for byte in data:
                    if discarding:
                        if byte == 10:
                            discarding = False
                        continue
                    if byte == 10:
                        self._process_line(bytes(buffer))
                        buffer.clear()
                    elif len(buffer) >= MAX_LINE_BYTES:
                        buffer.clear()
                        discarding = True
                        self._reject("串口行过长，已丢弃并等待下一完整行", time.monotonic())
                    else:
                        buffer.append(byte)
        except Exception as exc:
            if not self._stop.is_set():
                self._reject(f"串口读取中断：{exc}", time.monotonic())
                with self._lock:
                    self.connected = False
        finally:
            serial_port = self._serial
            if serial_port is not None:
                try:
                    serial_port.close()
                except Exception:
                    pass
