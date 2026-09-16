"""UDS 安全访问（0x27）测试。

安全访问是诊断里最容易被面试官追问的服务，测试要覆盖：
    - 正常流程：请求种子 -> 算密钥 -> 送密钥 -> 解锁
    - 密钥错误：回 0x35，且**不能**解锁
    - 未请求种子就送密钥：回 0x24（顺序错误）
    - 连续错误达上限：回 0x36 并进入延时锁定
    - 锁定期间重试：回 0x37
    - 解锁状态的生命周期：换会话 / 复位都要清掉
"""

from __future__ import annotations

import pytest

from src.uds import (
    Nrc,
    NegativeResponseError,
    SessionType,
    UdsClient,
    UdsServer,
    demo_seed_to_key,
)


# --------------------------------------------------------------------------- #
# 算法本身
# --------------------------------------------------------------------------- #


class TestSeedToKeyAlgorithm:
    def test_deterministic(self):
        seed = bytes([0x12, 0x34, 0x56, 0x78])
        assert demo_seed_to_key(seed) == demo_seed_to_key(seed)

    def test_different_seeds_give_different_keys(self):
        assert demo_seed_to_key(bytes([0, 0, 0, 1])) != demo_seed_to_key(bytes([0, 0, 0, 2]))

    def test_output_is_4_bytes(self):
        assert len(demo_seed_to_key(bytes([0xFF, 0xFF, 0xFF, 0xFF]))) == 4

    def test_rejects_wrong_seed_length(self):
        with pytest.raises(ValueError):
            demo_seed_to_key(bytes([1, 2, 3]))

    def test_known_vector(self):
        """固定向量，防止算法被无意改动。

        seed = 0x00000000
        mixed = (0 << 3) ^ 0xA5A5A5A5 = 0xA5A5A5A5
        key   = 0xA5A5A5A5 + 0x12345678 = 0xB7D9FC1D
        """
        assert demo_seed_to_key(bytes([0x00, 0x00, 0x00, 0x00])) == bytes(
            [0xB7, 0xD9, 0xFC, 0x1D]
        )


# --------------------------------------------------------------------------- #
# 正常流程
# --------------------------------------------------------------------------- #


class TestSecurityAccessHappyPath:
    def test_request_seed_returns_4_bytes(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        seed = client.security_access_request_seed(level=1)
        assert len(seed) == 4
        assert seed != bytes(4), "种子不应为全零（全零表示已解锁）"

    def test_seeds_differ_between_requests(self, client: UdsClient, ecu: UdsServer):
        """每次请求种子都应该不同，防止重放攻击。"""
        client.diagnostic_session_control(SessionType.EXTENDED)
        seeds = {client.security_access_request_seed(level=1) for _ in range(5)}
        assert len(seeds) > 1

    def test_unlock_flow(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        seed = client.security_access_request_seed(level=1)
        key = demo_seed_to_key(seed)
        response = client.security_access_send_key(key, level=1)

        assert response == bytes([0x67, 0x02])
        assert ecu.security_level == 1

    def test_unlock_helper_does_full_exchange(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.unlock(level=1)
        assert ecu.security_level == 1

    def test_level_2_independent_from_level_1(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.unlock(level=2)
        assert ecu.security_level == 2


# --------------------------------------------------------------------------- #
# 异常路径
# --------------------------------------------------------------------------- #


class TestSecurityAccessFailures:
    def test_wrong_key_nrc_0x35(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.security_access_request_seed(level=1)
        with pytest.raises(NegativeResponseError) as exc:
            client.security_access_send_key(bytes([0xDE, 0xAD, 0xBE, 0xEF]), level=1)
        assert exc.value.nrc == Nrc.INVALID_KEY
        assert ecu.security_level is None, "密钥错误绝不能解锁"

    def test_key_without_seed_nrc_0x24(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        with pytest.raises(NegativeResponseError) as exc:
            client.security_access_send_key(bytes([1, 2, 3, 4]), level=1)
        assert exc.value.nrc == Nrc.REQUEST_SEQUENCE_ERROR

    def test_wrong_key_length_nrc_0x13(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.security_access_request_seed(level=1)
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x27, 0x02, 0x01, 0x02]))  # 密钥只有 2 字节
        assert exc.value.nrc == Nrc.INCORRECT_MESSAGE_LENGTH

    def test_seed_request_with_extra_bytes_nrc_0x13(self, client: UdsClient):
        client.diagnostic_session_control(SessionType.EXTENDED)
        with pytest.raises(NegativeResponseError) as exc:
            client.request(bytes([0x27, 0x01, 0x00]))
        assert exc.value.nrc == Nrc.INCORRECT_MESSAGE_LENGTH

    def test_lockout_after_max_attempts(self, client: UdsClient, ecu: UdsServer):
        """连续 3 次错误密钥后，第 3 次应回 0x36 并锁定。"""
        client.diagnostic_session_control(SessionType.EXTENDED)
        wrong = bytes([0x00, 0x00, 0x00, 0x00])

        for attempt in range(2):
            client.security_access_request_seed(level=1)
            with pytest.raises(NegativeResponseError) as exc:
                client.security_access_send_key(wrong, level=1)
            assert exc.value.nrc == Nrc.INVALID_KEY, f"第 {attempt + 1} 次应为 0x35"

        client.security_access_request_seed(level=1)
        with pytest.raises(NegativeResponseError) as exc:
            client.security_access_send_key(wrong, level=1)
        assert exc.value.nrc == Nrc.EXCEED_NUMBER_OF_ATTEMPTS

    def test_locked_out_requests_rejected(self, client: UdsClient, ecu: UdsServer):
        """锁定期间连请求种子都要被拒，回 0x37。"""
        client.diagnostic_session_control(SessionType.EXTENDED)
        wrong = bytes([0x00, 0x00, 0x00, 0x00])
        for _ in range(3):
            try:
                client.security_access_request_seed(level=1)
                client.security_access_send_key(wrong, level=1)
            except NegativeResponseError:
                pass

        with pytest.raises(NegativeResponseError) as exc:
            client.security_access_request_seed(level=1)
        assert exc.value.nrc == Nrc.REQUIRED_TIME_DELAY_NOT_EXPIRED

    def test_can_unlock_after_delay_expires(self, connections):
        """锁定时间（测试里设为 0.3 s）过去后应能重新解锁。"""
        import time

        from src.isotp import IsoTpConnection
        from src.uds import UdsServerConfig

        tester_conn, ecu_conn = connections
        server = UdsServer(ecu_conn, UdsServerConfig(security_delay=0.3))
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=1.0)
            client.diagnostic_session_control(SessionType.EXTENDED)
            wrong = bytes([0x00, 0x00, 0x00, 0x00])
            for _ in range(3):
                try:
                    client.security_access_request_seed(level=1)
                    client.security_access_send_key(wrong, level=1)
                except NegativeResponseError:
                    pass

            time.sleep(0.5)
            client.unlock(level=1)
            assert server.security_level == 1
        finally:
            server.stop()

    def test_successful_unlock_resets_failure_counter(self, client: UdsClient, ecu: UdsServer):
        """一次失败后再成功，失败计数要清零，不能累积到锁定。"""
        client.diagnostic_session_control(SessionType.EXTENDED)

        client.security_access_request_seed(level=1)
        with pytest.raises(NegativeResponseError):
            client.security_access_send_key(bytes([0, 0, 0, 0]), level=1)

        client.unlock(level=1)

        # 再次解锁仍应成功（计数已清零）
        client.unlock(level=1)
        assert ecu.security_level == 1


# --------------------------------------------------------------------------- #
# 解锁状态的生命周期
# --------------------------------------------------------------------------- #


class TestSecurityStateLifecycle:
    def test_return_to_default_session_locks(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.unlock(level=1)
        assert ecu.security_level == 1

        client.diagnostic_session_control(SessionType.DEFAULT)
        assert ecu.security_level is None

    def test_ecu_reset_locks(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.unlock(level=1)
        client.ecu_reset()
        assert ecu.security_level is None

    def test_gated_service_after_relock(self, client: UdsClient, ecu: UdsServer):
        client.diagnostic_session_control(SessionType.EXTENDED)
        client.unlock(level=1)
        client.write_data_by_identifier(0x0101, bytes([0x02]))

        client.ecu_reset()

        client.diagnostic_session_control(SessionType.EXTENDED)
        with pytest.raises(NegativeResponseError) as exc:
            client.write_data_by_identifier(0x0101, bytes([0x03]))
        assert exc.value.nrc == Nrc.SECURITY_ACCESS_DENIED
