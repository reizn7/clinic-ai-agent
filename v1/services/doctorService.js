const Doctor = require("../models/Doctor");
const ApiError = require("../utils/ApiError");

/**
 * Doctor data access + business logic.
 */

const listActive = async () => Doctor.find({ isActive: true }).sort({ name: 1 });

const list = async ({ activeOnly } = {}) => {
    const query = {};
    if (activeOnly) query.isActive = true;
    return Doctor.find(query).sort({ name: 1 });
};

const findById = async (id) => Doctor.findById(id);

const create = async (payload) => Doctor.create(payload);

const update = async (id, payload) => {
    const doctor = await Doctor.findByIdAndUpdate(id, payload, {
        returnDocument: "after",
        runValidators: true,
    });
    if (!doctor) throw ApiError.notFound("Doctor not found");
    return doctor;
};

module.exports = { listActive, list, findById, create, update };
