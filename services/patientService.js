const Patient = require("../models/Patient");

/**
 * Patient data access + business logic.
 */

/**
 * Create the patient if new, or update their details if they already
 * exist (matched by phone). Used by the booking flow where the phone
 * number is always known from WhatsApp.
 */
const upsertByPhone = async (phone, { name, age, gender }) => {
    const update = {};
    if (name !== undefined) update.name = name;
    if (age !== undefined) update.age = age;
    if (gender !== undefined) update.gender = gender;

    return Patient.findOneAndUpdate(
        { phone },
        { $set: update, $setOnInsert: { phone } },
        { returnDocument: "after", upsert: true, setDefaultsOnInsert: true }
    );
};

const findByPhone = async (phone) => Patient.findOne({ phone });

const findById = async (id) => Patient.findById(id);

/**
 * List patients, optionally filtered by a search term matching name or
 * phone (case-insensitive). Powers the dashboard's "Search Patient".
 */
const list = async ({ search } = {}) => {
    const query = {};
    if (search) {
        const rx = new RegExp(escapeRegex(search), "i");
        query.$or = [{ name: rx }, { phone: rx }];
    }
    return Patient.find(query).sort({ createdAt: -1 });
};

const escapeRegex = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

module.exports = { upsertByPhone, findByPhone, findById, list };
