from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JevQuestion:
    text: str
    kind: str = "noul"
    options: object = None
    key: str = ""


@dataclass(frozen=True, slots=True)
class JevRequest:
    state: object
    question: JevQuestion
    state_id: str = ""


@dataclass(frozen=True, slots=True)
class JevResult:
    key: str
    answer: str
    probabilities: dict
    top_probability: float
    entropy_concentration: float
    input_tokens: int

    @property
    def p_yes(self):
        if set(self.probabilities) != {"false", "true"}:
            raise ValueError("p_yes is only available for noul results")
        return self.probabilities["true"]


class JevInputTooLong(ValueError):
    pass


class JevUnavailable(RuntimeError):
    pass


class JevOutOfMemory(RuntimeError):
    pass
