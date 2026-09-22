const mongoose = require("mongoose");
const { WEEKDAYS } = require("../constants");

const doctorSchema = new mongoose.Schema(
    {
        name: {
            type: String,
            required: true,
            trim: true,
        },
        specialization: {
            type: String,
            required: true,
            trim: true,
        },
        // Consultation length in minutes; drives slot generation.
        consultationDuration: {
            type: Number,
            default: 30,
            min: 5,
        },
        // Weekday names the doctor works, e.g. ["Monday", "Wednesday"].
        availableDays: {
            type: [String],
            enum: WEEKDAYS,
            default: [],
        },
        // Daily working window used to tile slots.
        workingHours: {
            start: { type: String, default: "09:00" }, // "HH:mm"
            end: { type: String, default: "17:00" }, // "HH:mm"
        },
        isActive: {
            type: Boolean,
            default: true,
            index: true,
        },
    },
    { timestamps: true }
);

module.exports = mongoose.model("Doctor", doctorSchema);
