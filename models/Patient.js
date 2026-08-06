const mongoose = require("mongoose");
const { GENDER_VALUES } = require("../constants");

const patientSchema = new mongoose.Schema(
    {
        // WhatsApp number is the natural unique identifier for a patient.
        phone: {
            type: String,
            required: true,
            unique: true,
            trim: true,
            index: true,
        },
        name: {
            type: String,
            required: true,
            trim: true,
        },
        age: {
            type: Number,
            min: 0,
            max: 120,
        },
        gender: {
            type: String,
            enum: GENDER_VALUES,
        },
    },
    { timestamps: true } // adds createdAt + updatedAt
);

module.exports = mongoose.model("Patient", patientSchema);
