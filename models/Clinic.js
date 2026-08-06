const mongoose = require("mongoose");

const clinicSchema = new mongoose.Schema(
    {
        clinicName: {
            type: String,
            required: true,
            trim: true,
        },
        // Human-readable timings string shown to patients, e.g.
        // "Mon-Sat, 9:00 AM - 5:00 PM".
        timings: {
            type: String,
            default: "",
        },
        address: {
            type: String,
            default: "",
        },
        phone: {
            type: String,
            default: "",
        },
        // Default slot length in minutes (doctors can override).
        slotDuration: {
            type: Number,
            default: 30,
            min: 5,
        },
    },
    { timestamps: true }
);

module.exports = mongoose.model("Clinic", clinicSchema);
