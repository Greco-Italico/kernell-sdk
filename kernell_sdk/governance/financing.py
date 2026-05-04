"""
Kernell OS SDK — Agent Credit & Financing
══════════════════════════════════════════
Underwriting automático para agentes.
Permite adelantos de caja, microcréditos y leasing de hardware.

Fórmula de Score de Crédito:
  C = 0.35R + 0.25E + 0.20U + 0.20H
  R: Reputación
  E: Earnings históricos normalizados
  U: Uptime
  H: Historial de cumplimiento (SLA/Deudas)
"""
from dataclasses import dataclass, field
import uuid

@dataclass
class LoanRequest:
    loan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    borrower_id: str = ""
    amount_kern: float = 0.0
    purpose: str = ""
    credit_score: float = 0.0
    interest_rate: float = 0.0
    status: str = "pending" # pending, approved, rejected, paid, defaulted

class FinancingEngine:
    W_REP = 0.35
    W_EARN = 0.25
    W_UPTIME = 0.20
    W_HISTORY = 0.20
    
    def __init__(self):
        self._loans = {}

    def calculate_credit_score(self, rep: float, earnings: float, uptime: float, history: float) -> float:
        """Calcula el score de crédito C = 0.35R + 0.25E + 0.20U + 0.20H"""
        # Normalizar earnings a 0-100 (asumiendo 10000 KERN = 100 max)
        e = min(100.0, (earnings / 10000.0) * 100.0)
        
        c = (self.W_REP * rep) + (self.W_EARN * e) + (self.W_UPTIME * uptime) + (self.W_HISTORY * history)
        return round(c, 2)

    def request_loan(self, borrower_id: str, amount: float, purpose: str, rep: float, earnings: float, uptime: float, history: float) -> LoanRequest:
        score = self.calculate_credit_score(rep, earnings, uptime, history)
        
        loan = LoanRequest(borrower_id=borrower_id, amount_kern=amount, purpose=purpose, credit_score=score)
        
        # Lógica de Underwriting automático
        if score >= 80.0:
            loan.status = "approved"
            loan.interest_rate = 0.05 # 5%
        elif score >= 50.0 and amount <= 500:
            loan.status = "approved"
            loan.interest_rate = 0.12 # 12%
        else:
            loan.status = "rejected"
            
        self._loans[loan.loan_id] = loan
        return loan
