from abc import ABC, abstractmethod

class BaseKMS(ABC):
    """
    Interface for Key Management Service.
    Implementations must securely hold keys and perform cryptographic operations
    without exposing the private key material to the application.
    """

    @abstractmethod
    def sign(self, key_id: str, payload: bytes) -> bytes:
        pass

    @abstractmethod
    def verify(self, key_id: str, payload: bytes, signature: bytes) -> bool:
        pass
