"""Clinic-configured data (clinic data contract): definitions, records, scoping."""

from __future__ import annotations

import mongomock
import pytest

from clinic_agent.config import settings
from clinic_agent.tools import catalog, repo

CLINIC = settings.CLINIC_ID
OTHER = "other-clinic"


def _field(key, type_, **kw):
    return {
        "key": key,
        "label": kw.pop("label", key.title()),
        "type": type_,
        "agentVisible": kw.pop("agentVisible", True),
        "pii": kw.pop("pii", False),
        "archived": kw.pop("archived", False),
        "order": kw.pop("order", 0),
        **kw,
    }


LAB_TEST_DEF = {
    "clinicId": CLINIC,
    "entityType": "lab_test",
    "kind": "custom",
    "label": "Lab tests",
    "singularLabel": "Lab test",
    "description": "Pathology tests with price and report time.",
    "agentEnabled": True,
    "titleField": "name",
    "fields": [
        _field("name", "text", label="Name", order=0),
        _field("price", "number", label="Price (INR)", order=1),
        _field("fastingRequired", "boolean", label="Fasting required", order=2),
        _field("internalCode", "text", label="Internal code", agentVisible=False, order=3),
        _field("oldNotes", "longtext", label="Old notes", archived=True, order=4),
    ],
}


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["catalog-test"]
    monkeypatch.setattr(repo, "get_db", lambda: fake)
    monkeypatch.setattr(catalog, "get_db", lambda: fake)
    catalog.clear_cache()
    fake["entity_definitions"].insert_many(
        [
            dict(LAB_TEST_DEF),
            {
                "clinicId": CLINIC,
                "entityType": "doctor",
                "kind": "system",
                "label": "Doctors",
                "fields": [
                    _field("degrees", "tags", label="Degrees", order=0),
                    _field("experience", "number", label="Experience (years)", order=1),
                    _field("email", "email", label="Email", pii=True, order=2),
                ],
            },
            {  # hidden from the agent
                "clinicId": CLINIC,
                "entityType": "staff_notes",
                "kind": "custom",
                "label": "Staff notes",
                "agentEnabled": False,
                "titleField": "title",
                "fields": [_field("title", "text")],
            },
            {**LAB_TEST_DEF, "clinicId": OTHER, "label": "Other clinic tests"},
        ]
    )
    fake["records"].insert_many(
        [
            {
                "clinicId": CLINIC,
                "entityType": "lab_test",
                "isActive": True,
                "attributes": {
                    "name": "Thyroid profile (T3 T4 TSH)",
                    "price": 650,
                    "fastingRequired": False,
                    "internalCode": "TH-01",
                    "oldNotes": "legacy",
                },
            },
            {
                "clinicId": CLINIC,
                "entityType": "lab_test",
                "isActive": True,
                "attributes": {"name": "Blood sugar fasting", "price": "120", "fastingRequired": True},
            },
            {
                "clinicId": CLINIC,
                "entityType": "lab_test",
                "isActive": False,
                "attributes": {"name": "Discontinued thyroid test", "price": 10},
            },
            {
                "clinicId": OTHER,
                "entityType": "lab_test",
                "isActive": True,
                "attributes": {"name": "Other clinic thyroid", "price": 1},
            },
        ]
    )
    return fake


# ---- definitions and field visibility --------------------------------------------


def test_only_agent_visible_non_pii_live_fields_are_rendered(db):
    defn = catalog.get_definitions()["lab_test"]
    out = catalog.render_attributes(
        defn,
        {"name": "CBC", "price": 350, "internalCode": "X", "oldNotes": "y", "extra": "z"},
    )
    assert out == {"Name": "CBC", "Price (INR)": 350}


def test_type_mismatches_are_dropped_not_raised(db):
    defn = catalog.get_definitions()["lab_test"]
    out = catalog.render_attributes(defn, {"name": "CBC", "price": "350", "fastingRequired": "no"})
    assert out == {"Name": "CBC"}  # string price / string boolean skipped


def test_definitions_are_per_clinic_and_cached(db):
    assert catalog.get_definitions()["lab_test"]["label"] == "Lab tests"
    assert catalog.get_definitions(OTHER)["lab_test"]["label"] == "Other clinic tests"
    db["entity_definitions"].update_one(
        {"clinicId": CLINIC, "entityType": "lab_test"}, {"$set": {"label": "Renamed"}}
    )
    assert catalog.get_definitions()["lab_test"]["label"] == "Lab tests"  # cached
    catalog.clear_cache()
    assert catalog.get_definitions()["lab_test"]["label"] == "Renamed"


# ---- catalog search --------------------------------------------------------------


def test_search_matches_active_records_of_this_clinic_only(db):
    res = catalog.search_catalog("lab_test", "thyroid")
    assert res["total_matches"] == 1
    item = res["results"][0]
    assert item["name"] == "Thyroid profile (T3 T4 TSH)"
    assert item["Price (INR)"] == 650
    assert item["Fasting required"] is False
    assert "Internal code" not in item and "Old notes" not in item


def test_search_by_label_and_empty_query_lists_all(db):
    res = catalog.search_catalog("Lab tests")
    assert res["catalog"] == "lab_test"
    assert [i["name"] for i in res["results"]] == [
        "Blood sugar fasting",
        "Thyroid profile (T3 T4 TSH)",
    ]
    # The bad (string) price is dropped for that row, the row still shows.
    assert "Price (INR)" not in res["results"][0]


def test_search_unknown_or_disabled_catalog_lists_the_available_ones(db):
    for name in ("vaccines", "staff_notes"):
        res = catalog.search_catalog(name, "x")
        assert res["available_catalogs"] == ["lab_test"]


def test_prompt_section_lists_agent_enabled_catalogs(db):
    text = catalog.prompt_section()
    assert "search_catalog" in text
    assert "- lab_test (Lab tests) — Pathology tests with price and report time." in text
    assert "staff_notes" not in text


def test_prompt_section_empty_without_catalogs(db):
    assert catalog.prompt_section("clinic-without-config") == ""


# ---- clinic scoping in the booking repo -------------------------------------------


def _doctor(clinic, name, **attrs):
    return {
        "clinicId": clinic,
        "name": name,
        "specialization": "General Physician",
        "isActive": True,
        "availableDays": ["Monday"],
        "workingHours": {"start": "09:00", "end": "10:00"},
        "consultationDuration": 30,
        "attributes": attrs,
    }


def test_doctors_are_scoped_and_carry_visible_details(db):
    db["doctors"].insert_many(
        [
            _doctor(CLINIC, "Asha Mehta", degrees=["MBBS", "MD"], experience=12, email="a@x.in"),
            _doctor(OTHER, "Other Doctor"),
            {**_doctor(None, "Legacy Unscoped")},
        ]
    )
    doctors = repo.list_active_doctors()
    assert [d["name"] for d in doctors] == ["Asha Mehta"]
    assert doctors[0]["details"] == {"Degrees": ["MBBS", "MD"], "Experience (years)": 12}


def test_doctor_lookup_strips_dr_prefix_and_escapes_regex(db):
    db["doctors"].insert_many([_doctor(CLINIC, "Asha Mehta"), _doctor(OTHER, "Rohit Sharma")])
    assert repo._find_doctor_by_name("Dr. Asha Mehta")["name"] == "Asha Mehta"
    assert repo._find_doctor_by_name("doctor asha mehta")["name"] == "Asha Mehta"
    assert repo._find_doctor_by_name(".*") is None  # not a wildcard
    assert repo._find_doctor_by_name("Rohit Sharma") is None  # other clinic


def test_clinic_info_is_scoped_with_details(db):
    db["entity_definitions"].insert_one(
        {
            "clinicId": CLINIC,
            "entityType": "clinic",
            "kind": "system",
            "fields": [_field("parking", "text", label="Parking")],
        }
    )
    catalog.clear_cache()
    db["clinic"].insert_many(
        [
            {"clinicId": OTHER, "clinicName": "Other"},
            {
                "clinicId": CLINIC,
                "clinicName": "Sunrise",
                "timings": "9-5",
                "attributes": {"parking": "Free, basement"},
            },
        ]
    )
    info = repo.get_clinic()
    assert info["clinicName"] == "Sunrise"
    assert info["details"] == {"Parking": "Free, basement"}
