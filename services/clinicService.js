const Clinic = require("../models/Clinic");

/**
 * Clinic profile service. The system assumes a single clinic; we always
 * read the first document. A sensible default is returned if none exists
 * yet so the conversation flow never crashes on a fresh database.
 */

const DEFAULT_CLINIC = {
    clinicName: "ABC Clinic",
    timings: "Mon-Sat, 9:00 AM - 5:00 PM",
    address: "Address not configured yet.",
    phone: "Reception number not configured yet.",
    slotDuration: 30,
};

const getClinic = async () => {
    const clinic = await Clinic.findOne().lean();
    return clinic || DEFAULT_CLINIC;
};

module.exports = { getClinic, DEFAULT_CLINIC };
