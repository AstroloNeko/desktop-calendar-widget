"""Calendar schema gate shared by storage and backup readers; never writes data."""
import json

DATA_SCHEMA_VERSION = 8


class DataCompatibilityError(ValueError):
    pass


class DataVersionTooNew(DataCompatibilityError):
    def __init__(self, found, supported, app_version):
        self.found, self.supported = found, supported
        minimum = "v0.9.2 或更新的兼容版本" if found == 8 else "支持此数据格式的较新版本"
        super().__init__(f"当前程序 v{app_version} 无法读取较新版本保存的日历数据。"
                         f"数据 Schema {found}，程序最高支持 {supported}。请使用{minimum}启动；不要恢复或重建日历。")


def decode_calendar_data(data: bytes, *, supported=DATA_SCHEMA_VERSION, app_version="unknown") -> dict:
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DataCompatibilityError("日历数据格式损坏或不完整，请保留原文件后检查。") from exc
    if not isinstance(raw, dict):
        raise DataCompatibilityError("日历数据根节点必须是对象。")
    schema = raw.get("version", 1)
    if type(schema) is not int or schema < 1:
        raise DataCompatibilityError("日历数据 Schema 字段无效。")
    if schema > supported:
        raise DataVersionTooNew(schema, supported, app_version)
    return raw
