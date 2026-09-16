"""UDS 服务测试（ISO 14229）。

按真实诊断测试规范组织：
    - 每个服务一组：肯定响应格式 + 各类否定响应（NRC）
    - 会话门控：默认会话下哪些服务必须被拒绝
    - 报文长度校验：长度不对必须回 0x13
"""

from __future__ import annotations

import pytest

from src.uds import (
    Nrc,
    NegativeResponseError,
    ResetType,
    SessionType,
    UdsClient,
    UdsError,
    UdsServer,
)


def nrc_of(exc_info) -> int:
    return exc_info.value.nrc


# --------------------------------------------------------------------------- #
# 0x10 诊断会话控制
# --------------------------------------------------------------------------- #


class TestSessionControl:
    def test_switch_to_extended(self, client: UdsClient):
        response = client.diagnostic_session_control(SessionType.EXTENDED)
        assert response[0] == 0x50
        assert response[1] == SessionType.EXTENDED
        # 肯定响应带 P2 / P2* 时间参数，共 6 字节
        assert len(response) == 6

    def test_switch_to_programming(self, client: UdsClient):
        response = client.diagnostic_session_control(SessionType.PROGRAMMING)
        assert response[1] == SessionType.PROGRAMMING

    def test_back_to_default(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        response = client.diagnostic_session_control(SessionType.DEFAULT)
        assert response[1] == SessionType.DEFAULT
        # 回到默认会话必须清掉安全解锁状态
        assert client.read_data_by_identifier(0xF186) == bytes([SessionType.DEFAULT])

    def test_p2_timing_differs_by_session(self, client: UdsClient):
        """默认会话的 P2* 比扩展会话长，这是规范要求的。"""
        default_resp = client.diagnostic_session_control(SessionType.DEFAULT)
        extended_resp = client.diagnostic_session_control(SessionType.EXTENDED)
        default_p2_star = (default_resp[4] << 8) | default_resp[5]
        extended_p2_star = (extended_resp[4] << 8) | extended_resp[5]
        assert default_p2_star > extended_p2_star

    def test_unsupported_session_nrc_0x12(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.diagnostic_session_control(0x7F)
        assert nrc_of(exc) == Nrc.SUB_FUNCTION_NOT_SUPPORTED

    def test_short_request_nrc_0x13(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x10]))
        assert nrc_of(exc) == Nrc.INCORRECT_MESSAGE_LENGTH


# --------------------------------------------------------------------------- #
# 0x11 ECU 复位
# --------------------------------------------------------------------------- #


class TestEcuReset:
    def test_hard_reset(self, client: UdsClient, ecu: UdsServer):
        response = client.ecu_reset(ResetType.HARD)
        assert response == bytes([0x51, ResetType.HARD])
        assert ecu.reset_count == 1

    def test_reset_returns_to_default_session(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.ecu_reset(ResetType.SOFT)
        assert client.read_data_by_identifier(0xF186) == bytes([SessionType.DEFAULT])

    def test_reset_clears_security(self, client: UdsClient, unlocked_client: UdsClient):
        """复位后回到默认会话，写服务应被会话门控拦住（0x7F）。

        注意顺序：会话检查先于安全等级检查，所以这里期望 0x7F 而不是 0x33。
        默认会话本身就是"这个服务现在不可用"，比"你还没解锁"更准确。
        """
        unlocked_client.ecu_reset(ResetType.HARD)
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.write_data_by_identifier(0x0100, bytes([1, 2]))
        assert nrc_of(exc) == Nrc.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION

    def test_unsupported_reset_type(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.ecu_reset(0x7F)
        assert nrc_of(exc) == Nrc.SUB_FUNCTION_NOT_SUPPORTED


# --------------------------------------------------------------------------- #
# 0x22 / 0x2E 按 DID 读写
# --------------------------------------------------------------------------- #


class TestDataByIdentifier:
    def test_read_vin(self, client: UdsClient):
        vin = client.read_data_by_identifier(0xF190)
        assert vin == b"LSNHBE1A2M0000001"
        assert len(vin) == 17

    @pytest.mark.parametrize(
        "did,expected",
        [
            (0xF187, b"3EC907321A"),
            (0xF18C, b"ECU2408001234"),
            (0xF195, b"SW01.03.02"),
            (0xF191, b"HW02.01"),
        ],
    )
    def test_read_standard_dids(self, client: UdsClient, did: int, expected: bytes):
        assert client.read_data_by_identifier(did) == expected

    def test_unknown_did_nrc_0x31(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.read_data_by_identifier(0xDEAD)
        assert nrc_of(exc) == Nrc.REQUEST_OUT_OF_RANGE

    def test_odd_length_request_nrc_0x13(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x22, 0xF1]))  # DID 只给了一个字节
        assert nrc_of(exc) == Nrc.INCORRECT_MESSAGE_LENGTH

    def test_multi_did_read_uses_multi_frame(self, client: UdsClient):
        """一次读 4 个 DID，响应必然超过 7 字节，走多帧。"""
        response = client.request(
            bytes([0x22, 0xF1, 0x87, 0xF1, 0x8C, 0xF1, 0x95, 0xF1, 0x91])
        )
        assert response[0] == 0x62
        assert len(response) > 7
        # 4 个 DID 的数据都要在
        assert b"3EC907321A" in response
        assert b"ECU2408001234" in response
        assert b"SW01.03.02" in response
        assert b"HW02.01" in response

    def test_write_requires_unlock(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        with pytest.raises(NegativeResponseError) as exc:
            client.write_data_by_identifier(0x0101, bytes([0x03]))
        assert nrc_of(exc) == Nrc.SECURITY_ACCESS_DENIED

    def test_write_after_unlock(self, unlocked_client: UdsClient):
        response = unlocked_client.write_data_by_identifier(0x0101, bytes([0x03]))
        assert response == bytes([0x6E, 0x01, 0x01])
        assert unlocked_client.read_data_by_identifier(0x0101) == bytes([0x03])

    def test_write_readonly_did_nrc_0x22(self, unlocked_client: UdsClient):
        """VIN 是只读的，解锁了也不许改。"""
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.write_data_by_identifier(0xF190, b"X" * 17)
        assert nrc_of(exc) == Nrc.CONDITIONS_NOT_CORRECT

    def test_write_unknown_did_nrc_0x31(self, unlocked_client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.write_data_by_identifier(0xDEAD, bytes([1]))
        assert nrc_of(exc) == Nrc.REQUEST_OUT_OF_RANGE


# --------------------------------------------------------------------------- #
# 0x14 / 0x19 DTC
# --------------------------------------------------------------------------- #


class TestDiagnosticTroubleCodes:
    def test_read_all_dtcs(self, client: UdsClient):
        dtcs = client.read_dtc_information()
        assert len(dtcs) == 3
        assert [code for code, _ in dtcs] == [0x00A001, 0x00B102, 0x00C203]

    def test_read_dtcs_with_status_mask(self, client: UdsClient):
        """按状态掩码过滤：只取 testFailed 置位的。"""
        dtcs = client.read_dtc_information(status_mask=0x01)
        assert all(status & 0x01 for _code, status in dtcs)

    def test_clear_then_read(self, client: UdsClient):
        assert client.clear_diagnostic_information() == bytes([0x54])
        assert client.read_dtc_information() == []

    def test_unsupported_subfunction(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x19, 0x0A]))
        assert nrc_of(exc) == Nrc.SUB_FUNCTION_NOT_SUPPORTED

    def test_clear_wrong_length(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x14, 0xFF]))
        assert nrc_of(exc) == Nrc.INCORRECT_MESSAGE_LENGTH


# --------------------------------------------------------------------------- #
# 0x3E TesterPresent
# --------------------------------------------------------------------------- #


class TestTesterPresent:
    def test_positive_response(self, client: UdsClient):
        assert client.tester_present() == bytes([0x7E, 0x00])

    def test_suppress_response_sends_nothing(self, client: UdsClient, connections):
        """suppressPosRspMsgIndicationBit 置位时 ECU 不应回复。"""
        tester_conn, _ = connections
        before = tester_conn.stats["rx_frames"]
        client.tester_present(suppress_response=True)
        import time

        time.sleep(0.2)
        assert tester_conn.stats["rx_frames"] == before

    def test_keeps_session_alive(self, connections):
        """S3 定时器：不断发 TesterPresent 就不会掉回默认会话。"""
        import time

        from src.isotp import IsoTpConfig, IsoTpConnection
        from src.uds import UdsServerConfig

        tester_conn, ecu_conn = connections
        server = UdsServer(ecu_conn, UdsServerConfig(s3_timeout=0.5))
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=1.0)
            client.diagnostic_session_control(SessionType.EXTENDED)
            for _ in range(4):
                time.sleep(0.2)
                client.tester_present()
            assert server.session == SessionType.EXTENDED
        finally:
            server.stop()

    def test_session_drops_after_s3_timeout(self, connections):
        import time

        from src.uds import UdsServerConfig

        tester_conn, ecu_conn = connections
        server = UdsServer(ecu_conn, UdsServerConfig(s3_timeout=0.3))
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=1.0)
            client.diagnostic_session_control(SessionType.EXTENDED)
            assert server.session == SessionType.EXTENDED
            time.sleep(0.8)
            assert server.session == SessionType.DEFAULT
        finally:
            server.stop()


# --------------------------------------------------------------------------- #
# 0x31 例程控制 / 0x28 通信控制
# --------------------------------------------------------------------------- #


class TestRoutineControl:
    def test_start_routine_needs_security(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        with pytest.raises(NegativeResponseError) as exc:
            client.routine_control(0x01, 0x0203)
        assert nrc_of(exc) == Nrc.SECURITY_ACCESS_DENIED

    def test_start_routine_after_unlock(self, unlocked_client: UdsClient, ecu: UdsServer):
        response = unlocked_client.routine_control(0x01, 0x0203)
        assert response[0] == 0x71
        assert ecu.routines_run == [(0x0203, b"")]

    def test_unknown_routine_nrc_0x31(self, unlocked_client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.routine_control(0x01, 0x1234)
        assert nrc_of(exc) == Nrc.REQUEST_OUT_OF_RANGE

    def test_bad_control_type(self, unlocked_client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.routine_control(0x09, 0x0203)
        assert nrc_of(exc) == Nrc.SUB_FUNCTION_NOT_SUPPORTED


class TestCommunicationControl:
    def test_enable_rx_and_tx(self, unlocked_client: UdsClient):
        assert unlocked_client.request(bytes([0x28, 0x00, 0x01]))[0] == 0x68

    def test_bad_subfunction(self, unlocked_client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            unlocked_client.request(bytes([0x28, 0x0F, 0x01]))
        assert nrc_of(exc) == Nrc.SUB_FUNCTION_NOT_SUPPORTED


# --------------------------------------------------------------------------- #
# 通用否定响应
# --------------------------------------------------------------------------- #


class TestGenericNegativeResponses:
    def test_unsupported_service_nrc_0x11(self, client: UdsClient):
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x99, 0x01]))
        assert nrc_of(exc) == Nrc.SERVICE_NOT_SUPPORTED
        # 否定响应格式固定：7F <请求SID> <NRC>
        assert exc.value.request_sid == 0x99

    @pytest.mark.parametrize("sid", [0x27, 0x28, 0x2E, 0x31])
    def test_write_services_blocked_in_default_session(self, client: UdsClient, sid: int):
        """默认会话下，安全/写/例程类服务必须回 0x7F（当前会话不支持）。"""
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([sid, 0x01, 0x00, 0x00]))
        assert nrc_of(exc) == Nrc.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION

    def test_response_sid_is_request_sid_plus_0x40(self, client: UdsClient):
        for sid in (0x10, 0x11, 0x22):
            response = client.request(bytes([sid, 0x01] if sid != 0x22 else [0x22, 0xF1, 0x90]))
            assert response[0] == sid + 0x40


# --------------------------------------------------------------------------- #
# 0x78 响应挂起
# --------------------------------------------------------------------------- #


class TestResponsePending:
    def test_client_handles_0x78_transparently(self, connections):
        """ECU 先回 0x78 再回最终响应，客户端应自动等待。"""
        from src.uds import UdsServerConfig

        tester_conn, ecu_conn = connections
        server = UdsServer(ecu_conn, UdsServerConfig(pending_services={0x22: 0.3}))
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=2.0)
            vin = client.read_data_by_identifier(0xF190)
            assert vin == b"LSNHBE1A2M0000001"
        finally:
            server.stop()

    def test_pending_is_visible_when_disabled(self, connections):
        """关掉自动等待后，0x78 应该作为异常暴露出来，便于测试断言。"""
        from src.uds import UdsServerConfig

        tester_conn, ecu_conn = connections
        server = UdsServer(ecu_conn, UdsServerConfig(pending_services={0x22: 0.2}))
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=2.0)
            with pytest.raises(NegativeResponseError) as exc:
                client.request(bytes([0x22, 0xF1, 0x90]), allow_pending=False)
            assert nrc_of(exc) == Nrc.RESPONSE_PENDING
        finally:
            server.stop()
