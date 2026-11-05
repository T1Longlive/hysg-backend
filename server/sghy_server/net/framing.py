"""协议帧编解码。

线型（与客户端/mock 完全一致，小端）：
  请求（客户端→服务端）：[4B 10008][4B len][4B opCode][4B protoIndex][protobuf]
                          len = 8 + len(pb)
  响应（服务端→客户端）：[4B len][4B opCode][protobuf]
                          len = 4 + len(pb)（无 magic、无 protoIndex）
  登录首包：裸 Base64(JSON + "\\n")，无长度头。
"""

import struct

MAGIC = 10008


def encode_frame(opcode: int, body: bytes) -> bytes:
    """编码一条服务端→客户端响应帧。"""
    payload = struct.pack("<i", opcode) + body
    return struct.pack("<i", len(payload)) + payload


class FrameDecoder:
    """增量帧解码器：feed 数据，逐条吐出 (opcode, proto_index, pb)。"""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        self._buf += chunk

    def next_message(self):
        """解码下一条请求帧；数据不完整返回 None。

        返回 ("BAD_MAGIC", None, None) 表示缓冲区已清空（ Magic 不符）。
        """
        buf = self._buf
        if len(buf) < 8:
            return None
        magic = struct.unpack_from("<i", buf, 0)[0]
        if magic != MAGIC:
            del buf[:]
            return ("BAD_MAGIC", None, None)
        length = struct.unpack_from("<i", buf, 4)[0]
        total = 8 + length
        if len(buf) < total:
            return None
        body = bytes(buf[8:total])
        del buf[:total]
        opcode = struct.unpack_from("<i", body, 0)[0]
        proto_index = struct.unpack_from("<i", body, 4)[0] if len(body) >= 8 else 0
        return opcode, proto_index, body[8:]
