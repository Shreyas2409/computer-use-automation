"""Tenant configuration. Differs only in route prefix, labels, and headings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    id: str
    display_name: str
    route_prefix: str
    search_button_label: str
    members_heading: str
    member_detail_heading: str
    subaccount_heading: str
    open_subaccount_button: str


TENANTS: dict[str, Tenant] = {
    "meridian": Tenant(
        id="meridian",
        display_name="Meridian Credit Union",
        route_prefix="",
        search_button_label="Search",
        members_heading="Member Search",
        member_detail_heading="Member Profile",
        subaccount_heading="Open Sub-Account",
        open_subaccount_button="Open Sub-Account",
    ),
    "summit": Tenant(
        id="summit",
        display_name="Summit Federal",
        route_prefix="/servicing",
        search_button_label="Find Member",
        members_heading="Member Lookup",
        member_detail_heading="Servicing Profile",
        subaccount_heading="New Sub-Account",
        open_subaccount_button="Create Sub-Account",
    ),
}


def resolve(name: str | None) -> Tenant:
    key = (name or "meridian").strip().lower()
    if key not in TENANTS:
        raise ValueError(
            f"Unknown tenant '{name}'. Expected one of: {sorted(TENANTS)}"
        )
    return TENANTS[key]
