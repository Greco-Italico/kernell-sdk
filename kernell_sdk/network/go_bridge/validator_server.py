import grpc
from concurrent import futures
import time
import sys
import os

# Note: In a real environment, we'd compile the proto first.
# For this architecture blueprint, we assume validator_pb2 and validator_pb2_grpc are generated.

# Mocking the imports for the blueprint
class MockValidatorPb2:
    class ValidationResponse:
        def __init__(self, valid, reason, reputation_delta, ban_peer):
            self.valid = valid
            self.reason = reason
            self.reputation_delta = reputation_delta
            self.ban_peer = ban_peer

class MockValidatorPb2Grpc:
    class ProtocolValidatorServicer:
        pass
    def add_ProtocolValidatorServicer_to_server(self, servicer, server):
        pass

validator_pb2 = MockValidatorPb2()
validator_pb2_grpc = MockValidatorPb2Grpc()

class ProtocolValidatorServicer(validator_pb2_grpc.ProtocolValidatorServicer):
    def ValidateMessage(self, request, context):
        # 🔐 Aquí conectas TODO tu motor real:
        # - firmas Ed25519
        # - epoch rules
        # - economic rules
        # - slashing triggers
        
        now_epoch = int(time.time()) // 5
        
        # ejemplo mínimo pero correcto
        if request.epoch > now_epoch + 1:
            return validator_pb2.ValidationResponse(
                valid=False,
                reason="epoch_from_future",
                reputation_delta=-10,
                ban_peer=False
            )
            
        if not request.msg_id or not request.type:
            return validator_pb2.ValidationResponse(
                valid=False,
                reason="malformed",
                reputation_delta=-20,
                ban_peer=True
            )
            
        # aquí luego:
        # verify_signature(...)
        # verify_economic_rules(...)
        # verify_receipt(...)
        
        return validator_pb2.ValidationResponse(
            valid=True,
            reason="ok",
            reputation_delta=1,
            ban_peer=False
        )

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    validator_pb2_grpc.add_ProtocolValidatorServicer_to_server(
        ProtocolValidatorServicer(), server
    )
    server.add_insecure_port('[::]:50051')
    server.start()
    print("Validator running on :50051")
    server.wait_for_termination()

if __name__ == "__main__":
    serve()
