"""Synthetic member data. No real names, no SSN-shaped fields."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    number_masked: str
    kind: str
    balance_cents: int

    @property
    def balance_display(self) -> str:
        dollars, cents = divmod(self.balance_cents, 100)
        return f"${dollars:,}.{cents:02d}"


@dataclass(frozen=True)
class Member:
    id: str
    first_name: str
    last_name: str
    status: str
    branch: str
    joined: str
    accounts: tuple[Account, ...]

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


MIN_OPENING_DEPOSIT_CENTS = 2500

_MEMBERS: dict[str, Member] = {
    "10001": Member(
        id="10001",
        first_name="Alex",
        last_name="Rivera",
        status="Active",
        branch="Downtown",
        joined="2018-04-12",
        accounts=(
            Account("****4417", "Checking", 285043),
            Account("****9022", "Savings", 1204588),
        ),
    ),
    "10002": Member(
        id="10002",
        first_name="Jordan",
        last_name="Blake",
        status="Restricted",
        branch="Northside",
        joined="2015-09-30",
        accounts=(
            Account("****3311", "Checking", 42017),
        ),
    ),
    "10003": Member(
        id="10003",
        first_name="Sam",
        last_name="Okafor",
        status="Active",
        branch="Riverside",
        joined="2020-01-05",
        accounts=(
            Account("****7182", "Checking", 512300),
            Account("****5540", "Savings", 89012),
            Account("****8801", "Money Market", 2350100),
        ),
    ),
}


def get_member(member_id: str) -> Member | None:
    return _MEMBERS.get((member_id or "").strip())


def list_members() -> list[Member]:
    return list(_MEMBERS.values())
