"""DBC 编解码与信号校验测试。

对应真实测试岗的三类用例：
    - 信号位布局测试：信号在报文里的起始位、长度对不对
    - 信号分辨率测试：物理值与原始值的换算对不对
    - 信号范围测试：越界值能不能被拦住
"""

from __future__ import annotations

import pytest

from src.dbc_codec import DbcCodec, DbcError, SignalRangeError


class TestDatabase:
    def test_messages_loaded(self, codec: DbcCodec):
        assert set(codec.message_names) == {
            "EngineData",
            "VehicleStatus",
            "BatteryData",
            "DoorStatus",
            "DiagnosticRequest",
            "DiagnosticResponse",
        }

    def test_frame_ids(self, codec: DbcCodec):
        assert codec.message("EngineData").frame_id == 0x100
        assert codec.message("VehicleStatus").frame_id == 0x101
        assert codec.message("BatteryData").frame_id == 0x200
        assert codec.message("DoorStatus").frame_id == 0x300
        assert codec.message("DiagnosticRequest").frame_id == 0x7E0
        assert codec.message("DiagnosticResponse").frame_id == 0x7E8

    def test_dlc(self, codec: DbcCodec):
        assert codec.expected_dlc("EngineData") == 8
        assert codec.expected_dlc("DoorStatus") == 4

    def test_unknown_message_raises(self, codec: DbcCodec):
        with pytest.raises(DbcError):
            codec.message("NotAMessage")

    def test_unknown_frame_id_raises(self, codec: DbcCodec):
        with pytest.raises(DbcError):
            codec.message_by_id(0x999)


class TestSignalSpecs:
    """信号布局与分辨率，逐一对照 DBC 定义。"""

    def test_engine_speed_layout(self, codec: DbcCodec):
        spec = next(s for s in codec.signal_specs("EngineData") if s.name == "EngineSpeed")
        assert spec.start_bit == 0
        assert spec.length == 16
        assert spec.byte_order == "Intel"
        assert spec.scale == 0.25
        assert spec.offset == 0
        assert spec.unit == "rpm"
        assert spec.bit_range == "0..15"

    def test_coolant_temp_has_offset(self, codec: DbcCodec):
        spec = next(s for s in codec.signal_specs("EngineData") if s.name == "CoolantTemp")
        assert spec.scale == 1
        assert spec.offset == -40  # 物理值 = 原始值 - 40
        assert spec.minimum == -40
        assert spec.maximum == 215

    def test_steering_angle_is_signed_range(self, codec: DbcCodec):
        spec = next(s for s in codec.signal_specs("VehicleStatus") if s.name == "SteeringAngle")
        assert spec.length == 16
        assert spec.minimum == -780
        assert spec.maximum == 780

    def test_odometer_is_24_bit(self, codec: DbcCodec):
        spec = next(s for s in codec.signal_specs("VehicleStatus") if s.name == "Odometer")
        assert spec.length == 24
        assert spec.bit_range == "32..55"

    def test_physical_raw_conversion(self, codec: DbcCodec):
        spec = next(s for s in codec.signal_specs("EngineData") if s.name == "EngineSpeed")
        assert spec.to_raw(2400.0) == 9600  # 2400 / 0.25
        assert spec.to_physical(9600) == 2400.0


class TestRoundTrip:
    def test_engine_data(self, codec: DbcCodec):
        signals = {
            "EngineSpeed": 2400.0,
            "VehicleSpeed": 72.5,
            "CoolantTemp": 88,
            "ThrottlePosition": 34.0,
            "EngineState": 2,
            "MilStatus": 0,
        }
        frame = codec.encode("EngineData", signals)
        assert frame.arbitration_id == 0x100
        assert len(frame.data) == 8

        decoded = codec.decode(frame)
        # 分辨率决定精度：0.25 rpm 的量化误差可忽略
        assert decoded["EngineSpeed"] == pytest.approx(2400.0)
        assert decoded["VehicleSpeed"] == pytest.approx(72.5)
        assert decoded["CoolantTemp"] == pytest.approx(88)
        assert decoded["ThrottlePosition"] == pytest.approx(34.0)
        assert decoded["EngineState"] == 2

    def test_engine_speed_raw_bit_pattern(self, codec: DbcCodec):
        """2400 rpm / 0.25 = 9600 = 0x2580，Intel 小端 -> 80 25。"""
        frame = codec.encode(
            "EngineData",
            {
                "EngineSpeed": 2400.0,
                "VehicleSpeed": 0.0,
                "CoolantTemp": -40,
                "ThrottlePosition": 0.0,
                "EngineState": 0,
                "MilStatus": 0,
            },
        )
        assert frame.data[0] == 0x80
        assert frame.data[1] == 0x25

    def test_battery_data(self, codec: DbcCodec):
        signals = {
            "PackVoltage": 398.5,
            "PackCurrent": 120.0,
            "StateOfCharge": 76.5,
            "BatteryTemp": 32,
            "CellVoltMax": 4.12,
            "CellVoltMin": 4.05,
        }
        frame = codec.encode("BatteryData", signals)
        decoded = codec.decode(frame)
        assert decoded["PackVoltage"] == pytest.approx(398.5, abs=0.01)
        assert decoded["StateOfCharge"] == pytest.approx(76.5)
        assert decoded["CellVoltMax"] == pytest.approx(4.12, abs=0.02)

    def test_negative_value_with_offset(self, codec: DbcCodec):
        """冷却液温度 -40°C 是物理最小值，原始值应为 0。"""
        frame = codec.encode(
            "EngineData",
            {
                "EngineSpeed": 0.0,
                "VehicleSpeed": 0.0,
                "CoolantTemp": -40,
                "ThrottlePosition": 0.0,
                "EngineState": 0,
                "MilStatus": 0,
            },
        )
        decoded = codec.decode(frame)
        assert decoded["CoolantTemp"] == -40

    def test_enum_names(self, codec: DbcCodec):
        frame = codec.encode(
            "EngineData",
            {
                "EngineSpeed": 0.0,
                "VehicleSpeed": 0.0,
                "CoolantTemp": 20,
                "ThrottlePosition": 0.0,
                "EngineState": 2,
                "MilStatus": 1,
            },
        )
        named = codec.decode_with_names(frame)
        assert named["EngineState"] == "Running"
        assert named["MilStatus"] == "On"

    def test_door_status_4_bytes(self, codec: DbcCodec):
        frame = codec.encode(
            "DoorStatus",
            {
                "FrontLeftDoor": 1,
                "FrontRightDoor": 0,
                "RearLeftDoor": 1,
                "RearRightDoor": 0,
                "TrunkStatus": 0,
                "HoodStatus": 0,
                "LightStatus": 3,
            },
        )
        assert len(frame.data) == 4
        decoded = codec.decode(frame)
        assert decoded["FrontLeftDoor"] == 1
        assert decoded["RearLeftDoor"] == 1
        assert decoded["LightStatus"] == 3


class TestRangeValidation:
    """信号范围测试：这是 ECU 测试报告里最常见的一类用例。"""

    def test_over_max_rejected(self, codec: DbcCodec):
        with pytest.raises(SignalRangeError) as exc_info:
            codec.encode(
                "EngineData",
                {
                    "EngineSpeed": 20000.0,  # 上限 16383.75
                    "VehicleSpeed": 0.0,
                    "CoolantTemp": 20,
                    "ThrottlePosition": 0.0,
                    "EngineState": 0,
                    "MilStatus": 0,
                },
            )
        err = exc_info.value
        assert err.signal == "EngineSpeed"
        assert err.value == 20000.0
        assert err.high == 16383.75

    def test_under_min_rejected(self, codec: DbcCodec):
        with pytest.raises(SignalRangeError) as exc_info:
            codec.encode(
                "EngineData",
                {
                    "EngineSpeed": 0.0,
                    "VehicleSpeed": 0.0,
                    "CoolantTemp": -100,  # 下限 -40
                    "ThrottlePosition": 0.0,
                    "EngineState": 0,
                    "MilStatus": 0,
                },
            )
        assert exc_info.value.signal == "CoolantTemp"

    def test_boundary_values_accepted(self, codec: DbcCodec):
        """边界值必须能通过 —— 边界测试的基本要求。"""
        frame = codec.encode(
            "EngineData",
            {
                "EngineSpeed": 16383.75,  # 正好上限
                "VehicleSpeed": 0.0,
                "CoolantTemp": 215,  # 正好上限
                "ThrottlePosition": 100.0,  # 正好上限
                "EngineState": 0,
                "MilStatus": 0,
            },
        )
        decoded = codec.decode(frame)
        assert decoded["CoolantTemp"] == 215
        assert decoded["ThrottlePosition"] == pytest.approx(100.0)

    def test_validate_returns_all_violations(self, codec: DbcCodec):
        violations = codec.validate(
            "EngineData",
            {
                "EngineSpeed": 99999.0,
                "CoolantTemp": -999,
            },
        )
        assert len(violations) == 2
        assert {v.signal for v in violations} == {"EngineSpeed", "CoolantTemp"}

    def test_validate_decoded_catches_out_of_range(self, codec: DbcCodec):
        """对已解码报文做二次校验（解析 ECU 上报数据时的用法）。"""
        decoded = {"PackVoltage": 999.0}  # 上限 655.35
        violations = codec.validate_decoded("BatteryData", decoded)
        assert len(violations) == 1
        assert violations[0].signal == "PackVoltage"

    def test_unknown_signal_rejected(self, codec: DbcCodec):
        with pytest.raises(DbcError, match="unknown signals"):
            codec.encode("EngineData", {"NotASignal": 1})

    def test_strict_false_skips_validation(self, codec: DbcCodec):
        """strict=False 时不拦越界值，用于故意构造异常报文的负向测试。"""
        frame = codec.encode(
            "DoorStatus",
            {
                "FrontLeftDoor": 3,  # DBC 定义 0..3，本身就是边界
                "FrontRightDoor": 0,
                "RearLeftDoor": 0,
                "RearRightDoor": 0,
                "TrunkStatus": 0,
                "HoodStatus": 0,
                "LightStatus": 0,
            },
            strict=False,
        )
        assert frame.arbitration_id == 0x300
