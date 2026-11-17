"""Building blocks shared by the inbound/outbound contract and the three agents' business outputs.

The field names and limits are the ones ContractGuard exchanges with the worker, so a value an agent produces can be
copied into the result without renaming. Outbound blocks forbid unknown keys: the partner rejects them.
"""
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_serializer

NonEmpty = Annotated[str, Field(min_length=1)]
Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
ClauseType = Annotated[str, Field(pattern=r"^C(0[1-9]|1[0-9]|2[01]|99)$", description="BR-RISK-006; C99 until the catalog is provided")]
CheckCode = Annotated[str, Field(pattern=r"^(CY-(0[1-9]|1[0-2])|PR-0[1-5]|TP-0[1-3])$", description="BR-RISK-007")]
FactKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Domain = Literal["legal", "financial", "operational", "compliance", "cybersecurity", "data_privacy", "third_party"]
RiskLevel = Literal["low", "medium", "high", "critical"]
Severity = RiskLevel
Language = Literal["vi", "en", "mixed"]
ContractType = Literal["service_agreement", "vendor_agreement", "purchase_agreement", "msa", "sow", "other"]
# The 20 checks of BR-RISK-007: every result carries each code exactly once.
CHECK_CODES = tuple([f"CY-{i:02d}" for i in range(1, 13)] + [f"PR-{i:02d}" for i in range(1, 6)]
                    + [f"TP-{i:02d}" for i in range(1, 4)])


class Strict(BaseModel):
    """What we send or what an agent must answer: unknown keys are an error."""
    model_config = ConfigDict(extra="forbid")


class Evidence(Strict):
    clause_id: NonEmpty
    page: int = Field(ge=1)
    quote: NonEmpty = Field(description="Verbatim, contiguous text of that clause")


class Reference(Strict):
    source: NonEmpty = Field(description="Where it came from: law name/citation, evidence_id or URL returned by a tool")
    reference_number: NonEmpty = Field(description="Document symbol + article, e.g. '91/2015/QH13 Điều 418'")


class Fact(Strict):
    status: Literal["FOUND", "NOT_FOUND", "UNKNOWN"]
    value: Union[bool, int, float, str, None]
    evidence: list[Evidence]

    @model_serializer(mode="wrap")
    def _value_always_present(self, handler):
        data = handler(self)
        data["value"] = self.value  # required key even when null, also under exclude_none
        return data


class Check(Strict):
    check_code: CheckCode
    check_status: Literal["pass", "partial", "missing", "not_applicable"]
    clause_ids: list[str] = Field(default_factory=list)
    finding_id: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class ContractMetadata(Strict):
    """Every field may be null when the contract does not state it."""
    parties: list[NonEmpty] = Field(default_factory=list)
    contract_type: Optional[ContractType] = None
    contract_value: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")
    effective_date: Optional[str] = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    expiry_date: Optional[str] = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    governing_law: Optional[str] = Field(default=None, max_length=200)


class InjectionWarning(Strict):
    """A passage of the contract that reads like an instruction to the AI (never followed)."""
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=2000)


def check_codes_once(checks) -> list[str]:
    """Problems when `checks` is not exactly the 20 codes, each once."""
    codes = [check.check_code for check in checks]
    if sorted(codes) == sorted(CHECK_CODES):
        return []
    return [f"checks must hold the 20 codes once: missing {sorted(set(CHECK_CODES) - set(codes))}, "
            f"repeated {sorted({c for c in codes if codes.count(c) > 1})}"]
