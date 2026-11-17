"""Fill this clinic with realistic multi-speciality hospital data.

Adds, for ``CLINIC_ID`` (clinic data contract, see the console repo's
docs/clinic-data-contract.md):

* **Clinic page**: content for the ``about`` field plus extra agent-visible
  fields (emergency, ambulance helpline, visiting hours, beds, insurance, …).
* **Doctors**: a doctor page definition (degrees, experience, languages, fee,
  about, OPD room) and 14 more doctors across specialities.
* **Staff** page (not shown to the agent): who works where; staff with
  "Receives handoff alerts" on get a WhatsApp alert when a patient asks for a
  person (``runtime.escalation``). Seeded WITHOUT phone numbers and with alerts
  off: add real numbers in the console and switch alerts on.
* **Services & charges** page: consultations, lab tests, imaging, cardiac
  tests, health packages, wards and rooms, ICU, ambulance, emergency,
  procedures, dental, physiotherapy, with prices in INR.
* **Subsidies & schemes** page: discounts, government schemes, eligibility.

Run:  uv run python scripts/seed_hospital.py
Safe to re-run: it only inserts what is missing. Anything already there
(edited in the console or not) is left as is; clinic attributes and fields are
added only when absent, and existing doctors only get attributes if they have none.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dotenv import load_dotenv

load_dotenv()

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.tools import catalog  # noqa: E402
from clinic_agent.tools.repo import ensure_indexes  # noqa: E402


def _field(key: str, label: str, type_: str, order: int, **kw) -> dict:
    return {
        "key": key,
        "label": label,
        "type": type_,
        "required": kw.pop("required", False),
        "options": kw.pop("options", []),
        "description": kw.pop("description", ""),
        "agentVisible": kw.pop("agentVisible", True),
        "filterable": kw.pop("filterable", False),
        "pii": kw.pop("pii", False),
        "archived": False,
        "order": order,
    }


# ---- clinic -------------------------------------------------------------------------

ABOUT = (
    "MediDesk is a 150-bed NABH-accredited multi-speciality hospital in Sector 128, "
    "Noida, caring for patients since 2009. We offer OPD consultations in 16 "
    "specialities, a 24x7 emergency and trauma unit, a 20-bed ICU and NICU, four "
    "modular operation theatres, an NABL-accredited lab, CT, MRI and digital X-ray, "
    "a 24x7 pharmacy and our own ambulance fleet. Cashless treatment is available "
    "with major insurers, CGHS, ECHS and Ayushman Bharat."
)

CLINIC_FIELDS = [
    _field(
        "emergencyServices",
        "Emergency services",
        "text",
        1,
        description="What emergency care is available and when.",
    ),
    _field("ambulanceHelpline", "Ambulance helpline", "phone", 2),
    _field("visitingHours", "Visiting hours", "text", 3),
    _field("totalBeds", "Total beds", "number", 4),
    _field("specialities", "Specialities", "tags", 5),
    _field("facilities", "Facilities", "tags", 6),
    _field("accreditations", "Accreditations", "tags", 7),
    _field("insuranceAccepted", "Insurance / TPAs accepted (cashless)", "tags", 8),
    _field("paymentModes", "Payment modes", "tags", 9),
    _field("parking", "Parking", "text", 10),
    _field("followUpPolicy", "Follow-up policy", "text", 11),
]


def clinic_attributes(clinic_phone: str) -> dict:
    return {
        "about": ABOUT,
        "emergencyServices": (
            "24x7 emergency and trauma care with an emergency physician always on duty, "
            "ICU backup and in-house CT. Walk in any time; no appointment needed."
        ),
        # The reception number doubles as the ambulance helpline until a dedicated
        # line is configured (never invent a number patients might dial).
        "ambulanceHelpline": clinic_phone,
        "visitingHours": (
            "Wards and rooms: 11 AM-1 PM and 5-7 PM, one visitor pass per patient. "
            "ICU: 12-12:30 PM and 6-6:30 PM, one visitor at a time."
        ),
        "totalBeds": 150,
        "specialities": [
            "General Medicine",
            "Paediatrics",
            "Cardiology",
            "Dermatology",
            "Orthopaedics",
            "Obstetrics & Gynaecology",
            "ENT",
            "Ophthalmology",
            "Neurology",
            "Endocrinology",
            "Gastroenterology",
            "Psychiatry",
            "Pulmonology",
            "General Surgery",
            "Urology",
            "Dentistry",
        ],
        "facilities": [
            "24x7 Emergency",
            "ICU & NICU",
            "Modular OTs",
            "NABL lab",
            "CT & MRI",
            "Digital X-ray",
            "24x7 Pharmacy",
            "Blood storage unit",
            "Dialysis unit",
            "Physiotherapy",
            "Cafeteria",
            "Wheelchair access",
            "Free Wi-Fi",
        ],
        "accreditations": ["NABH", "NABL (laboratory)"],
        "insuranceAccepted": [
            "Star Health",
            "HDFC ERGO",
            "ICICI Lombard",
            "Niva Bupa",
            "Care Health",
            "Aditya Birla Health",
            "Bajaj Allianz",
            "Tata AIG",
            "Medi Assist (TPA)",
            "Paramount (TPA)",
            "CGHS",
            "ECHS",
            "Ayushman Bharat PM-JAY",
        ],
        "paymentModes": ["Cash", "UPI", "Debit / credit card", "Net banking", "Cashless insurance"],
        "parking": "Free basement parking for patients (2 hours); valet at the main gate.",
        "followUpPolicy": "One free follow-up visit with the same doctor within 7 days.",
    }


# ---- doctors ------------------------------------------------------------------------

LANGUAGES = ["English", "Hindi", "Punjabi", "Urdu", "Bengali", "Tamil", "Malayalam", "Marathi"]

DOCTOR_FIELDS = [
    _field("degrees", "Degrees", "tags", 0),
    _field("experience", "Experience (years)", "number", 1),
    _field("languages", "Languages", "multiselect", 2, options=LANGUAGES, filterable=True),
    _field("consultationFee", "Consultation fee (INR)", "number", 3),
    _field("about", "About", "longtext", 4),
    _field("opdRoom", "OPD room", "text", 5),
]

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _doctor(name, spec, days, start, end, minutes, attrs) -> dict:
    return {
        "name": name,
        "specialization": spec,
        "availableDays": days,
        "workingHours": {"start": start, "end": end},
        "consultationDuration": minutes,
        "isActive": True,
        "calendarEmail": None,
        "attributes": attrs,
    }


# Attributes for the two doctors that already exist (only applied if they have none).
EXISTING_DOCTOR_ATTRIBUTES = {
    "Asha Mehta": {
        "degrees": ["MBBS", "MD (Internal Medicine)"],
        "experience": 12,
        "languages": ["English", "Hindi"],
        "consultationFee": 600,
        "about": "Treats fever, infections, diabetes, blood pressure and general health concerns.",
        "opdRoom": "OPD 101, Ground floor",
    },
    "Rohit Sharma": {
        "degrees": ["BDS", "MDS (Endodontics)"],
        "experience": 8,
        "languages": ["English", "Hindi"],
        "consultationFee": 400,
        "about": "Root canals, fillings, scaling, extractions and smile care.",
        "opdRoom": "Dental wing, Ground floor",
    },
}

DOCTORS = [
    _doctor(
        "Priya Nair",
        "Pediatrician",
        [*WEEKDAYS, "Saturday"],
        "10:00",
        "13:00",
        15,
        {
            "degrees": ["MBBS", "MD (Paediatrics)"],
            "experience": 10,
            "languages": ["English", "Hindi", "Malayalam"],
            "consultationFee": 700,
            "about": "Newborn and child care, vaccinations, growth and nutrition.",
            "opdRoom": "OPD 104, Ground floor",
        },
    ),
    _doctor(
        "Vikram Rao",
        "Cardiologist",
        ["Tuesday", "Thursday", "Saturday"],
        "11:00",
        "15:00",
        30,
        {
            "degrees": ["MBBS", "MD (Medicine)", "DM (Cardiology)"],
            "experience": 18,
            "languages": ["English", "Hindi"],
            "consultationFee": 1200,
            "about": "Chest pain, heart failure, hypertension, angiography and angioplasty.",
            "opdRoom": "Heart Centre, 1st floor",
        },
    ),
    _doctor(
        "Sneha Kulkarni",
        "Dermatologist",
        ["Monday", "Wednesday", "Friday"],
        "14:00",
        "17:00",
        20,
        {
            "degrees": ["MBBS", "MD (Dermatology)"],
            "experience": 9,
            "languages": ["English", "Hindi", "Marathi"],
            "consultationFee": 900,
            "about": "Acne, eczema, psoriasis, hair loss, allergies and skin infections.",
            "opdRoom": "OPD 203, 2nd floor",
        },
    ),
    _doctor(
        "Arjun Menon",
        "Orthopedic Surgeon",
        WEEKDAYS,
        "09:00",
        "13:00",
        20,
        {
            "degrees": ["MBBS", "MS (Orthopaedics)"],
            "experience": 15,
            "languages": ["English", "Hindi", "Malayalam"],
            "consultationFee": 1000,
            "about": "Fractures, joint pain, sports injuries, knee and hip replacement.",
            "opdRoom": "OPD 110, Ground floor",
        },
    ),
    _doctor(
        "Fatima Sheikh",
        "Gynecologist",
        ["Tuesday", "Thursday", "Saturday"],
        "09:00",
        "13:00",
        30,
        {
            "degrees": ["MBBS", "MS (Obstetrics & Gynaecology)"],
            "experience": 14,
            "languages": ["English", "Hindi", "Urdu"],
            "consultationFee": 900,
            "about": "Pregnancy care, deliveries, PCOS, menstrual problems and infertility.",
            "opdRoom": "Mother & Child wing, 1st floor",
        },
    ),
    _doctor(
        "Sameer Bhatia",
        "ENT Specialist",
        ["Monday", "Wednesday", "Friday"],
        "10:00",
        "13:00",
        20,
        {
            "degrees": ["MBBS", "MS (ENT)"],
            "experience": 11,
            "languages": ["English", "Hindi", "Punjabi"],
            "consultationFee": 700,
            "about": "Ear infections, hearing loss, sinusitis, tonsils and voice problems.",
            "opdRoom": "OPD 206, 2nd floor",
        },
    ),
    _doctor(
        "Neha Agarwal",
        "Ophthalmologist",
        ["Tuesday", "Thursday", "Saturday"],
        "14:00",
        "17:00",
        20,
        {
            "degrees": ["MBBS", "MS (Ophthalmology)"],
            "experience": 9,
            "languages": ["English", "Hindi"],
            "consultationFee": 700,
            "about": "Eye check-ups, cataract surgery, glaucoma and diabetic eye care.",
            "opdRoom": "Eye Centre, 2nd floor",
        },
    ),
    _doctor(
        "Karan Malhotra",
        "Neurologist",
        ["Monday", "Thursday"],
        "12:00",
        "16:00",
        30,
        {
            "degrees": ["MBBS", "MD (Medicine)", "DM (Neurology)"],
            "experience": 16,
            "languages": ["English", "Hindi", "Punjabi"],
            "consultationFee": 1200,
            "about": "Headache, migraine, epilepsy, stroke, Parkinson's and nerve disorders.",
            "opdRoom": "OPD 301, 3rd floor",
        },
    ),
    _doctor(
        "Deepa Iyer",
        "Endocrinologist",
        ["Wednesday", "Saturday"],
        "10:00",
        "14:00",
        30,
        {
            "degrees": ["MBBS", "MD (Medicine)", "DM (Endocrinology)"],
            "experience": 12,
            "languages": ["English", "Hindi", "Tamil"],
            "consultationFee": 1000,
            "about": "Diabetes, thyroid, obesity, PCOS and hormone disorders.",
            "opdRoom": "OPD 302, 3rd floor",
        },
    ),
    _doctor(
        "Manish Verma",
        "Gastroenterologist",
        ["Tuesday", "Friday"],
        "14:00",
        "17:00",
        30,
        {
            "degrees": ["MBBS", "MD (Medicine)", "DM (Gastroenterology)"],
            "experience": 13,
            "languages": ["English", "Hindi"],
            "consultationFee": 1100,
            "about": "Acidity, liver disease, IBS, endoscopy and colonoscopy.",
            "opdRoom": "OPD 303, 3rd floor",
        },
    ),
    _doctor(
        "Ritu Saxena",
        "Psychiatrist",
        ["Monday", "Wednesday"],
        "14:00",
        "17:00",
        45,
        {
            "degrees": ["MBBS", "MD (Psychiatry)"],
            "experience": 10,
            "languages": ["English", "Hindi"],
            "consultationFee": 1500,
            "about": "Anxiety, depression, sleep problems, addiction and counselling.",
            "opdRoom": "Wellness Centre, 3rd floor",
        },
    ),
    _doctor(
        "Imran Qureshi",
        "Pulmonologist",
        ["Tuesday", "Thursday"],
        "09:00",
        "12:00",
        20,
        {
            "degrees": ["MBBS", "MD (Pulmonary Medicine)"],
            "experience": 11,
            "languages": ["English", "Hindi", "Urdu"],
            "consultationFee": 900,
            "about": "Asthma, COPD, TB, chronic cough, allergies and sleep apnoea.",
            "opdRoom": "OPD 207, 2nd floor",
        },
    ),
    _doctor(
        "Anjali Singh",
        "General Surgeon",
        ["Monday", "Thursday", "Saturday"],
        "09:00",
        "12:00",
        20,
        {
            "degrees": ["MBBS", "MS (General Surgery)", "FMAS"],
            "experience": 14,
            "languages": ["English", "Hindi", "Bengali"],
            "consultationFee": 800,
            "about": "Hernia, gallstones, appendix, piles and laparoscopic surgery.",
            "opdRoom": "OPD 112, Ground floor",
        },
    ),
    _doctor(
        "Harish Bansal",
        "Urologist",
        ["Wednesday", "Friday"],
        "10:00",
        "13:00",
        20,
        {
            "degrees": ["MBBS", "MS (General Surgery)", "MCh (Urology)"],
            "experience": 17,
            "languages": ["English", "Hindi", "Punjabi"],
            "consultationFee": 1000,
            "about": "Kidney stones, prostate problems, urinary infections and incontinence.",
            "opdRoom": "OPD 304, 3rd floor",
        },
    ),
]


# ---- staff (not agent-visible) ------------------------------------------------------

STAFF_ROLES = [
    "Receptionist",
    "Front desk manager",
    "Patient care coordinator",
    "Nurse",
    "Billing executive",
    "Insurance / TPA desk",
    "Ambulance coordinator",
    "Administrator",
]

STAFF_DEFINITION = {
    "entityType": "staff",
    "kind": "custom",
    "label": "Staff",
    "singularLabel": "Staff member",
    "agentEnabled": False,
    "titleField": "name",
    "description": (
        "The clinic team. Staff with 'Receives handoff alerts' on get a WhatsApp alert "
        "(with the patient's number) when a patient asks to talk to a person."
    ),
    "fields": [
        _field("name", "Name", "text", 0, required=True, agentVisible=False),
        _field(
            "role",
            "Role",
            "select",
            1,
            options=STAFF_ROLES,
            filterable=True,
            agentVisible=False,
        ),
        _field("department", "Department", "text", 2, agentVisible=False),
        _field(
            "phone",
            "WhatsApp number",
            "phone",
            3,
            agentVisible=False,
            pii=True,
            description="With country code, e.g. +91 98765 43210. Alerts are sent here.",
        ),
        _field("email", "Email", "email", 4, agentVisible=False, pii=True),
        _field(
            "receivesEscalations",
            "Receives handoff alerts",
            "boolean",
            5,
            agentVisible=False,
            filterable=True,
            description="WhatsApp this person when a patient asks for a human.",
        ),
        _field("shift", "Shift", "text", 6, agentVisible=False),
        _field("extension", "Internal extension", "text", 7, agentVisible=False),
        _field("notes", "Notes", "longtext", 8, agentVisible=False),
    ],
}

STAFF = [
    {
        "name": "Anita Sharma",
        "role": "Front desk manager",
        "department": "Front office",
        "shift": "Mon-Sat, 9 AM-5 PM",
        "extension": "101",
        "receivesEscalations": False,
        "notes": "Owns patient callbacks and complaints. Turn on alerts once the number is added.",
    },
    {
        "name": "Rahul Yadav",
        "role": "Receptionist",
        "department": "Front office",
        "shift": "Mon-Sat, 9 AM-5 PM",
        "extension": "102",
        "receivesEscalations": False,
    },
    {
        "name": "Pooja Rawat",
        "role": "Patient care coordinator",
        "department": "Front office",
        "shift": "Mon-Sat, 12 PM-8 PM",
        "extension": "103",
        "receivesEscalations": False,
    },
    {
        "name": "Suresh Pal",
        "role": "Billing executive",
        "department": "Billing",
        "shift": "Mon-Sat, 9 AM-6 PM",
        "extension": "120",
        "receivesEscalations": False,
    },
    {
        "name": "Meenakshi Rao",
        "role": "Insurance / TPA desk",
        "department": "Billing",
        "shift": "Mon-Sat, 9 AM-6 PM",
        "extension": "121",
        "receivesEscalations": False,
        "notes": "Cashless pre-authorisation, CGHS/ECHS and PM-JAY claims.",
    },
    {
        "name": "Vinod Kumar",
        "role": "Ambulance coordinator",
        "department": "Emergency",
        "shift": "24x7 rota",
        "extension": "108",
        "receivesEscalations": False,
    },
    {
        "name": "Sister Grace Thomas",
        "role": "Nurse",
        "department": "Nursing (ward in-charge)",
        "shift": "Rotational",
        "extension": "140",
        "receivesEscalations": False,
    },
]


# ---- services & charges ---------------------------------------------------------------

SERVICE_CATEGORIES = [
    "Consultation",
    "Lab test",
    "Radiology & imaging",
    "Cardiac test",
    "Health package",
    "Ward & room",
    "ICU & critical care",
    "Ambulance",
    "Emergency",
    "Surgery & procedure",
    "Dental",
    "Physiotherapy",
    "Other",
]

SERVICE_DEFINITION = {
    "entityType": "service",
    "kind": "custom",
    "label": "Services & charges",
    "singularLabel": "Service",
    "agentEnabled": True,
    "titleField": "name",
    "description": (
        "Everything the hospital offers with its price in INR: consultations, lab tests, "
        "scans, cardiac tests, health packages, ward and room rent, ICU, ambulance, "
        "emergency, surgery packages, dental and physiotherapy."
    ),
    "fields": [
        _field("name", "Name", "text", 0, required=True),
        _field("category", "Category", "select", 1, options=SERVICE_CATEGORIES, filterable=True),
        _field("price", "Price (INR)", "number", 2),
        _field("priceUnit", "Charged", "text", 3, description="e.g. per test, per day, per trip"),
        _field("description", "Details", "longtext", 4),
        _field("includes", "Includes", "tags", 5),
        _field("availability", "Availability", "text", 6),
        _field("fastingRequired", "Fasting required", "boolean", 7),
        _field("preparation", "Preparation", "text", 8),
        _field("reportTime", "Report time", "text", 9),
        _field("insuranceCovered", "Covered by insurance (inpatient)", "boolean", 10),
    ],
}


def _svc(category: str, name: str, price: float, unit: str, **kw) -> dict:
    return {"category": category, "name": name, "price": price, "priceUnit": unit, **kw}


LAB = "Lab test"
LAB_HOURS = "Sample collection Mon-Sat 7 AM-7 PM, Sun 7-11 AM; emergency samples 24x7"
IMG = "Radiology & imaging"
WARD = "Ward & room"
WARD_NOTE = (
    "Room rent covers the bed, nursing care, housekeeping and meals for the patient. "
    "Medicines, consumables, tests and doctor visits are billed separately."
)

SERVICES = [
    # Consultations
    _svc(
        "Consultation",
        "General physician OPD consultation",
        600,
        "per visit",
        availability="Mon-Sat, 9 AM-5 PM",
        description="One free follow-up with the same doctor within 7 days.",
    ),
    _svc(
        "Consultation",
        "Specialist OPD consultation",
        900,
        "per visit (Rs 700-1,500 depending on the doctor)",
        availability="Mon-Sat, as per each doctor's OPD days",
        description="Exact fee is listed with each doctor. Free follow-up within 7 days.",
    ),
    _svc(
        "Consultation",
        "Video teleconsultation",
        500,
        "per consultation",
        availability="Mon-Sat, 9 AM-5 PM",
        description="Video call with a doctor; prescription shared on WhatsApp.",
    ),
    _svc(
        "Consultation",
        "Doctor home visit",
        2000,
        "per visit (within 10 km)",
        availability="Mon-Sat, 10 AM-4 PM, book a day ahead",
    ),
    _svc(
        "Consultation",
        "Second opinion review of reports",
        1000,
        "per case",
        description="A specialist reviews existing reports and scans.",
    ),
    # Lab tests
    _svc(
        LAB,
        "Complete blood count (CBC)",
        350,
        "per test",
        fastingRequired=False,
        reportTime="Same day (6 hours)",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Blood sugar fasting (FBS)",
        120,
        "per test",
        fastingRequired=True,
        preparation="8-10 hours fasting; water is fine.",
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Blood sugar post-meal (PPBS)",
        120,
        "per test",
        fastingRequired=False,
        preparation="Sample exactly 2 hours after a meal.",
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "HbA1c (3-month sugar average)",
        550,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Lipid profile (cholesterol)",
        600,
        "per test",
        fastingRequired=True,
        preparation="10-12 hours fasting.",
        reportTime="Same day evening",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Thyroid profile (T3, T4, TSH)",
        650,
        "per test",
        fastingRequired=False,
        reportTime="Next day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Liver function test (LFT)",
        700,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Kidney function test (KFT)",
        750,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Vitamin D (25-OH)",
        1400,
        "per test",
        fastingRequired=False,
        reportTime="Next day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Vitamin B12",
        1000,
        "per test",
        fastingRequired=False,
        reportTime="Next day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Urine routine and microscopy",
        200,
        "per test",
        fastingRequired=False,
        preparation="Early-morning midstream sample preferred.",
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Stool routine",
        250,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Dengue NS1 antigen",
        900,
        "per test",
        fastingRequired=False,
        reportTime="Same day (4 hours)",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Malaria antigen (rapid)",
        400,
        "per test",
        fastingRequired=False,
        reportTime="2 hours",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Typhoid test (Widal)",
        300,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "C-reactive protein (CRP)",
        500,
        "per test",
        fastingRequired=False,
        reportTime="Same day",
        availability=LAB_HOURS,
    ),
    _svc(
        LAB,
        "Home sample collection",
        200,
        "per visit (free on bills above Rs 1,500)",
        availability="Daily, 7-11 AM, within 10 km",
    ),
    # Radiology & imaging
    _svc(
        IMG,
        "Digital X-ray chest (PA view)",
        450,
        "per X-ray",
        reportTime="1 hour",
        availability="24x7",
    ),
    _svc(
        IMG,
        "Ultrasound whole abdomen",
        1500,
        "per scan",
        fastingRequired=True,
        preparation="6 hours fasting; come with a full bladder.",
        reportTime="Same day",
        availability="Mon-Sat, 9 AM-6 PM",
    ),
    _svc(
        IMG,
        "Pregnancy ultrasound (obstetric)",
        1600,
        "per scan",
        reportTime="Same day",
        availability="Mon-Sat, 9 AM-6 PM",
        description="Level-II anomaly scan is Rs 3,000.",
    ),
    _svc(
        IMG,
        "CT scan head (plain)",
        3000,
        "per scan",
        reportTime="2 hours",
        availability="24x7",
    ),
    _svc(
        IMG,
        "HRCT chest",
        4500,
        "per scan",
        reportTime="Same day",
        availability="24x7",
    ),
    _svc(
        IMG,
        "MRI brain (plain)",
        7500,
        "per scan",
        preparation="Remove all metal; tell us about pacemakers or implants.",
        reportTime="Next day",
        availability="Mon-Sat, 8 AM-8 PM",
    ),
    _svc(
        IMG,
        "MRI knee or spine",
        8000,
        "per region",
        preparation="Remove all metal; tell us about pacemakers or implants.",
        reportTime="Next day",
        availability="Mon-Sat, 8 AM-8 PM",
    ),
    _svc(
        IMG,
        "Mammography (both breasts)",
        2500,
        "per test",
        preparation="No deodorant or powder on the day of the test.",
        reportTime="Next day",
        availability="Mon-Sat, 9 AM-4 PM",
    ),
    _svc(
        IMG,
        "Bone density scan (DEXA)",
        2000,
        "per scan",
        reportTime="Same day",
        availability="Mon-Sat, 9 AM-4 PM",
    ),
    # Cardiac
    _svc(
        "Cardiac test",
        "ECG",
        300,
        "per test",
        reportTime="Immediate",
        availability="24x7",
    ),
    _svc(
        "Cardiac test",
        "2D Echo",
        2500,
        "per test",
        reportTime="Same day",
        availability="Mon-Sat, 10 AM-4 PM",
    ),
    _svc(
        "Cardiac test",
        "Treadmill test (TMT)",
        2200,
        "per test",
        preparation="Light meal 2 hours before; wear sports shoes.",
        reportTime="Same day",
        availability="Mon-Sat, 10 AM-2 PM",
    ),
    _svc(
        "Cardiac test",
        "Holter monitoring (24 hours)",
        3000,
        "per test",
        reportTime="2 days",
    ),
    # Health packages
    _svc(
        "Health package",
        "Basic health check-up",
        1999,
        "per person",
        fastingRequired=True,
        preparation="10-12 hours fasting; come before 10 AM.",
        reportTime="Same day evening",
        includes=[
            "CBC",
            "Fasting sugar",
            "Lipid profile",
            "LFT",
            "KFT",
            "Urine routine",
            "ECG",
            "Physician consultation",
        ],
    ),
    _svc(
        "Health package",
        "Comprehensive health check-up",
        4999,
        "per person",
        fastingRequired=True,
        preparation="10-12 hours fasting; come before 10 AM.",
        reportTime="Next day",
        includes=[
            "Everything in Basic",
            "Thyroid profile",
            "HbA1c",
            "Vitamin D",
            "Vitamin B12",
            "Chest X-ray",
            "Ultrasound abdomen",
            "2D Echo",
            "Diet consultation",
        ],
    ),
    _svc(
        "Health package",
        "Senior citizen health check-up (60+)",
        3499,
        "per person",
        fastingRequired=True,
        reportTime="Next day",
        includes=[
            "CBC",
            "Sugar + HbA1c",
            "Lipid",
            "LFT",
            "KFT",
            "Thyroid",
            "Vitamin D",
            "ECG",
            "Chest X-ray",
            "Eye check-up",
            "Physician consultation",
        ],
    ),
    _svc(
        "Health package",
        "Women's wellness package",
        3999,
        "per person",
        fastingRequired=True,
        reportTime="Next day",
        includes=[
            "CBC",
            "Thyroid",
            "Vitamin D",
            "Iron studies",
            "Pap smear",
            "Mammography (40+) or breast ultrasound",
            "Pelvic ultrasound",
            "Gynaecologist consultation",
        ],
    ),
    _svc(
        "Health package",
        "Diabetes care package",
        1499,
        "per person",
        fastingRequired=True,
        reportTime="Same day",
        includes=[
            "Fasting and post-meal sugar",
            "HbA1c",
            "KFT",
            "Urine microalbumin",
            "Lipid profile",
            "Endocrinologist consultation",
        ],
    ),
    _svc(
        "Health package",
        "Pre-employment check-up",
        1200,
        "per person",
        reportTime="Same day",
        includes=[
            "CBC",
            "Blood group",
            "Sugar",
            "Urine routine",
            "Chest X-ray",
            "Fitness certificate",
        ],
    ),
    # Wards & rooms
    _svc(
        WARD,
        "General ward bed",
        1800,
        "per day",
        description=f"6-bed ward, shared washroom. {WARD_NOTE} Admission deposit Rs 10,000.",
        includes=["Bed", "Nursing care", "Meals", "Attendant chair"],
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Semi-private room (twin sharing)",
        3500,
        "per day",
        description=f"2 beds, AC, attached washroom. {WARD_NOTE} Admission deposit Rs 20,000.",
        includes=["AC", "Attached washroom", "Attendant bed", "Meals", "Nursing care"],
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Private room (single)",
        5500,
        "per day",
        description=f"Single AC room. {WARD_NOTE} Admission deposit Rs 30,000.",
        includes=[
            "AC",
            "TV",
            "Fridge",
            "Attendant sofa-bed",
            "Meals for patient and attendant",
            "Nursing care",
        ],
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Deluxe suite",
        9000,
        "per day",
        description=f"Suite with a separate sitting room. {WARD_NOTE} Admission deposit Rs 50,000.",
        includes=[
            "AC",
            "TV",
            "Fridge",
            "Sitting room",
            "Attendant bed",
            "Meals for patient and attendant",
        ],
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Isolation room",
        6000,
        "per day",
        description=f"Single room for infectious patients. {WARD_NOTE}",
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Day-care bed (up to 8 hours)",
        1500,
        "per session",
        description="For dialysis, chemotherapy, infusions and minor procedures; no overnight stay.",
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Doctor visit for admitted patients",
        700,
        "per visit (Rs 700 general ward, Rs 1,000 private and above)",
        insuranceCovered=True,
    ),
    _svc(
        WARD,
        "Admission and registration",
        500,
        "one time",
        description="Covers the file, ID band and medical records.",
    ),
    # ICU
    _svc(
        "ICU & critical care",
        "ICU bed",
        9000,
        "per day",
        description="Bed, monitoring, 1:1 or 1:2 nursing. Ventilator and medicines extra.",
        availability="24x7",
        insuranceCovered=True,
    ),
    _svc(
        "ICU & critical care",
        "High dependency unit (HDU)",
        6000,
        "per day",
        availability="24x7",
        insuranceCovered=True,
    ),
    _svc(
        "ICU & critical care",
        "NICU (newborn intensive care)",
        7000,
        "per day",
        availability="24x7",
        insuranceCovered=True,
    ),
    _svc(
        "ICU & critical care",
        "Ventilator support",
        4000,
        "per day",
        insuranceCovered=True,
    ),
    # Ambulance
    _svc(
        "Ambulance",
        "Basic life support (BLS) ambulance",
        1500,
        "per trip up to 10 km, then Rs 30 per km",
        availability="24x7, call the ambulance helpline",
        description="Oxygen, stretcher and a trained attendant.",
    ),
    _svc(
        "Ambulance",
        "Advanced life support (ALS) ambulance",
        3500,
        "per trip up to 10 km, then Rs 60 per km",
        availability="24x7, call the ambulance helpline",
        description="Cardiac monitor, defibrillator, ventilator and a paramedic; a doctor on request (Rs 1,500 extra).",
    ),
    _svc(
        "Ambulance",
        "Patient transport van (non-emergency)",
        1000,
        "per trip up to 10 km, then Rs 20 per km",
        availability="Daily, 7 AM-9 PM, book 2 hours ahead",
        description="For discharge, dialysis visits and check-ups; wheelchair and stretcher.",
    ),
    _svc(
        "Ambulance",
        "Hearse / mortuary van",
        2000,
        "per trip within Noida and Delhi NCR",
        availability="24x7",
    ),
    # Emergency
    _svc(
        "Emergency",
        "Emergency registration and doctor consultation",
        1000,
        "per visit",
        availability="24x7",
        description="Triage and examination by the emergency physician. Tests and medicines extra.",
    ),
    _svc("Emergency", "Wound dressing (minor)", 300, "per dressing", availability="24x7"),
    _svc("Emergency", "Stitches (suturing)", 1000, "per wound", availability="24x7"),
    _svc("Emergency", "Nebulisation", 250, "per session", availability="24x7"),
    _svc("Emergency", "Plaster cast (below elbow or knee)", 1500, "per cast", availability="24x7"),
    # Surgeries & procedures (packages, general ward)
    _svc(
        "Surgery & procedure",
        "Normal delivery package",
        45000,
        "package (2 days, general ward)",
        description="Includes delivery, room, routine medicines and newborn check. Private room +Rs 12,000.",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Caesarean (C-section) package",
        85000,
        "package (3 days, general ward)",
        description="Private room +Rs 18,000. NICU stay, if needed, is extra.",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Cataract surgery (phaco, monofocal lens)",
        28000,
        "per eye, day care",
        description="Premium foldable / multifocal lens options Rs 40,000-75,000.",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Laparoscopic gallbladder removal",
        75000,
        "package (2 days, general ward)",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Laparoscopic appendix removal",
        65000,
        "package (2 days, general ward)",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Laparoscopic hernia repair (with mesh)",
        70000,
        "package (2 days, general ward)",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Total knee replacement (one knee)",
        225000,
        "package (5 days, semi-private)",
        description="Includes implant (standard), surgery, stay and in-hospital physiotherapy.",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Coronary angiography",
        15000,
        "package (day care)",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Angioplasty with one drug-eluting stent",
        160000,
        "package (3 days)",
        description="Stent at the government-capped price. Each extra stent is charged separately.",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Haemodialysis",
        2200,
        "per session",
        availability="Mon-Sat, 7 AM-9 PM; book in advance",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Chemotherapy day care",
        3000,
        "per session (drugs extra)",
        insuranceCovered=True,
    ),
    _svc(
        "Surgery & procedure",
        "Upper GI endoscopy",
        3500,
        "per procedure",
        fastingRequired=True,
        preparation="6 hours fasting.",
    ),
    _svc(
        "Surgery & procedure",
        "Colonoscopy",
        6500,
        "per procedure",
        preparation="Bowel preparation the day before, as advised by the doctor.",
    ),
    # Dental
    _svc("Dental", "Scaling and polishing", 1200, "per sitting"),
    _svc("Dental", "Tooth-coloured filling", 1000, "per tooth"),
    _svc("Dental", "Root canal treatment", 4500, "per tooth (crown extra)"),
    _svc("Dental", "Tooth extraction", 800, "per tooth (wisdom tooth surgery Rs 4,000)"),
    _svc("Dental", "Ceramic crown", 6000, "per tooth"),
    # Physiotherapy
    _svc(
        "Physiotherapy",
        "Physiotherapy session",
        600,
        "per session (10 sessions for Rs 5,000)",
        availability="Mon-Sat, 8 AM-7 PM",
    ),
    _svc("Physiotherapy", "Home physiotherapy", 1000, "per session (within 10 km)"),
    # Other
    _svc(
        "Other",
        "Blood unit (packed red cells)",
        1550,
        "per unit (processing charge)",
        availability="24x7 blood storage unit",
        description="Replacement donors are encouraged.",
    ),
    _svc("Other", "Medical / fitness certificate", 300, "per certificate"),
    _svc(
        "Other",
        "Copy of medical records",
        100,
        "per page (discharge summary free)",
        availability="Medical records desk, Mon-Sat 10 AM-4 PM",
    ),
    _svc(
        "Other",
        "Child vaccination (administration)",
        200,
        "per dose, plus vaccine cost",
        availability="Mon-Sat, 10 AM-1 PM",
        description="Government-schedule vaccines are free; optional vaccines at MRP.",
    ),
]


# ---- subsidies & schemes ---------------------------------------------------------------

SCHEME_DEFINITION = {
    "entityType": "scheme",
    "kind": "custom",
    "label": "Subsidies & schemes",
    "singularLabel": "Scheme",
    "agentEnabled": True,
    "titleField": "name",
    "description": (
        "Discounts, free-care programmes and government or insurance schemes the hospital "
        "accepts: who is eligible, what is covered and which documents to bring."
    ),
    "fields": [
        _field("name", "Name", "text", 0, required=True),
        _field("benefit", "Benefit", "text", 1),
        _field("discountPercent", "Discount (%)", "number", 2),
        _field("eligibility", "Who is eligible", "longtext", 3),
        _field("appliesTo", "Applies to", "tags", 4),
        _field("documentsRequired", "Documents to bring", "tags", 5),
        _field("howToAvail", "How to avail", "text", 6),
    ],
}

SCHEMES = [
    {
        "name": "Senior citizen discount",
        "benefit": "15% off OPD consultations and lab tests; 10% off room rent.",
        "discountPercent": 15,
        "eligibility": "Patients aged 60 and above.",
        "appliesTo": ["OPD consultation", "Lab tests", "Room rent"],
        "documentsRequired": ["Aadhaar or any age proof"],
        "howToAvail": "Show age proof at billing. Not combinable with other discounts.",
    },
    {
        "name": "Ayushman Bharat (PM-JAY)",
        "benefit": "Cashless treatment up to Rs 5 lakh per family per year for covered procedures.",
        "eligibility": (
            "Families with an Ayushman card under PM-JAY, and all citizens aged 70+ "
            "with an Ayushman Vay Vandana card."
        ),
        "appliesTo": ["Inpatient admission", "Surgery packages", "Dialysis", "Chemotherapy"],
        "documentsRequired": ["Ayushman card", "Aadhaar", "Ration card (if asked)"],
        "howToAvail": "Visit the Ayushman desk near billing (ground floor) before admission.",
    },
    {
        "name": "CGHS and ECHS cashless treatment",
        "benefit": "Cashless OPD referrals and admission at CGHS/ECHS rates.",
        "eligibility": "Central government employees, pensioners and ex-servicemen with a valid card.",
        "appliesTo": ["Referred OPD", "Investigations", "Inpatient admission"],
        "documentsRequired": ["CGHS / ECHS card", "Referral letter", "Photo ID"],
        "howToAvail": "Bring the referral to the insurance / TPA desk.",
    },
    {
        "name": "Economically weaker section (EWS) free treatment",
        "benefit": "Free beds and treatment on 10% of beds; diagnostics at 50% off.",
        "eligibility": "Families with annual income under Rs 2.5 lakh, subject to bed availability.",
        "appliesTo": ["General ward admission", "OPD consultation", "Diagnostics"],
        "documentsRequired": ["Income certificate", "Aadhaar", "Ration card"],
        "howToAvail": "Apply at the medical social worker's desk (ground floor), Mon-Sat 10 AM-4 PM.",
    },
    {
        "name": "BPL card holder concession",
        "benefit": "50% off lab tests and imaging; dialysis at Rs 1,500 per session.",
        "discountPercent": 50,
        "eligibility": "Holders of a valid Below Poverty Line (BPL) card.",
        "appliesTo": ["Lab tests", "Imaging", "Dialysis"],
        "documentsRequired": ["BPL card", "Aadhaar"],
        "howToAvail": "Show the BPL card at billing.",
    },
    {
        "name": "Road accident victims: cashless emergency care",
        "benefit": (
            "Cashless treatment up to Rs 1.5 lakh for the first 7 days under the "
            "government scheme; free ambulance pick-up within 15 km."
        ),
        "eligibility": "Anyone injured in a road accident. No deposit is asked for in emergencies.",
        "appliesTo": ["Emergency care", "Ambulance", "Surgery", "ICU"],
        "documentsRequired": [
            "None needed to start treatment; police intimation is done by the hospital"
        ],
        "howToAvail": "Come straight to Emergency or call the ambulance helpline.",
    },
    {
        "name": "Armed forces and police discount",
        "benefit": "10% off OPD consultations, tests and room rent.",
        "discountPercent": 10,
        "eligibility": "Serving armed forces, CAPF and state police personnel and their dependants.",
        "appliesTo": ["OPD consultation", "Lab tests", "Imaging", "Room rent"],
        "documentsRequired": ["Service ID card"],
        "howToAvail": "Show the service ID at billing.",
    },
    {
        "name": "Free follow-up consultation",
        "benefit": "One free follow-up visit with the same doctor.",
        "discountPercent": 100,
        "eligibility": "Any patient, within 7 days of a paid OPD consultation.",
        "appliesTo": ["OPD consultation"],
        "documentsRequired": ["Previous OPD receipt"],
        "howToAvail": "Book a normal appointment; billing waives the fee.",
    },
    {
        "name": "Free child vaccination (government schedule)",
        "benefit": "Government-schedule vaccines and their administration are free.",
        "discountPercent": 100,
        "eligibility": "Children under 5 following the national immunisation schedule.",
        "appliesTo": ["Vaccination"],
        "documentsRequired": ["Vaccination card (MCP card)"],
        "howToAvail": "Walk in to the paediatric OPD, Mon-Sat 10 AM-1 PM.",
    },
    {
        "name": "Corporate and insurance cashless",
        "benefit": "Cashless admission with insurers and TPAs on our panel.",
        "eligibility": "Policy holders of a panel insurer or TPA (see the clinic's insurance list).",
        "appliesTo": ["Inpatient admission", "Day-care procedures"],
        "documentsRequired": ["Insurance / TPA card", "Photo ID", "Policy copy"],
        "howToAvail": (
            "Planned admission: send documents to the TPA desk 2-3 days ahead. "
            "Emergency: pre-authorisation is started within 24 hours of admission."
        ),
    },
]


# ---- writers ------------------------------------------------------------------------------


def _upsert_definition(db, clinic_id: str, definition: dict, iso: str) -> str:
    """Insert the definition, or append any of its fields the stored one lacks."""
    col = db[catalog.DEFINITIONS]
    key = {"clinicId": clinic_id, "entityType": definition["entityType"]}
    existing = col.find_one(key)
    if existing is None:
        col.insert_one({**definition, **key, "version": 1, "createdAt": iso, "updatedAt": iso})
        return "created"
    have = {f.get("key") for f in existing.get("fields") or []}
    next_order = 1 + max((f.get("order", 0) for f in existing.get("fields") or []), default=-1)
    new = []
    for f in definition["fields"]:
        if f["key"] not in have:
            new.append({**f, "order": next_order})
            next_order += 1
    if not new:
        return "unchanged"
    col.update_one(
        {"_id": existing["_id"]},
        {"$push": {"fields": {"$each": new}}, "$inc": {"version": 1}, "$set": {"updatedAt": iso}},
    )
    return f"+{len(new)} fields"


def _insert_records(db, clinic_id: str, etype: str, rows: list[dict], iso: str) -> int:
    added = 0
    for attrs in rows:
        attrs = {k: v for k, v in attrs.items() if v is not None}
        res = db[catalog.RECORDS].update_one(
            {"clinicId": clinic_id, "entityType": etype, "attributes.name": attrs["name"]},
            {
                "$setOnInsert": {
                    "clinicId": clinic_id,
                    "entityType": etype,
                    "isActive": True,
                    "attributes": attrs,
                    "createdAt": iso,
                    "updatedAt": iso,
                }
            },
            upsert=True,
        )
        added += res.upserted_id is not None
    return added


def main() -> None:
    db = get_db()
    clinic_id = settings.CLINIC_ID
    now = datetime.now(UTC)
    iso = now.isoformat()
    assert len(ABOUT) <= 500, f"about is {len(ABOUT)} chars; text fields max 500"
    report: list[str] = []

    # Clinic: fields + attribute values, only where missing.
    clinic_def = {
        "entityType": "clinic",
        "kind": "system",
        "label": "Clinic",
        "singularLabel": "Clinic",
        "description": "Clinic information.",
        "fields": [_field("about", "About", "text", 0), *CLINIC_FIELDS],
    }
    report.append(f"clinic definition: {_upsert_definition(db, clinic_id, clinic_def, iso)}")
    clinic = db["clinic"].find_one({"clinicId": clinic_id})
    if clinic is None:
        raise SystemExit(
            f"No clinic document for clinicId={clinic_id!r}; run scripts/seed.py first."
        )
    attrs = clinic.get("attributes") or {}
    filled = {
        f"attributes.{k}": v
        for k, v in clinic_attributes(str(clinic.get("phone") or "")).items()
        if attrs.get(k) in (None, "", [])
    }
    if filled:
        db["clinic"].update_one({"_id": clinic["_id"]}, {"$set": {**filled, "updatedAt": iso}})
    report.append(f"clinic attributes filled: {sorted(k.split('.', 1)[1] for k in filled)}")

    # Doctors.
    report.append(
        "doctor definition: "
        + _upsert_definition(
            db,
            clinic_id,
            {
                "entityType": "doctor",
                "kind": "system",
                "label": "Doctors",
                "singularLabel": "Doctor",
                "description": "Doctors patients can book.",
                "fields": DOCTOR_FIELDS,
            },
            iso,
        )
    )
    for name, extra in EXISTING_DOCTOR_ATTRIBUTES.items():
        db["doctors"].update_one(
            {
                "clinicId": clinic_id,
                "name": name,
                "$or": [
                    {"attributes": {"$exists": False}},
                    {"attributes": None},
                    {"attributes": {}},
                ],
            },
            {"$set": {"attributes": extra, "updatedAt": now}},
        )
    added_doctors = 0
    for doc in DOCTORS:
        res = db["doctors"].update_one(
            {"clinicId": clinic_id, "name": doc["name"]},
            {"$setOnInsert": {**doc, "clinicId": clinic_id, "createdAt": now, "updatedAt": now}},
            upsert=True,
        )
        added_doctors += res.upserted_id is not None
    report.append(f"doctors added: {added_doctors} (of {len(DOCTORS)})")

    # Custom pages.
    for definition, rows in (
        (STAFF_DEFINITION, STAFF),
        (SERVICE_DEFINITION, SERVICES),
        (SCHEME_DEFINITION, SCHEMES),
    ):
        etype = definition["entityType"]
        state = _upsert_definition(db, clinic_id, definition, iso)
        added = _insert_records(db, clinic_id, etype, rows, iso)
        report.append(
            f"{definition['label']} ({etype}): definition {state}, rows added {added}/{len(rows)}"
        )

    ensure_indexes()
    print(f"Hospital seed for db='{db.name}', clinicId='{clinic_id}':")
    for line in report:
        print(f"  - {line}")


if __name__ == "__main__":
    main()
