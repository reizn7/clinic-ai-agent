/**
 * Optional dev seed: creates one clinic and a couple of doctors so the
 * WhatsApp booking flow and dashboard have data to work with.
 *
 * Run with:  npm run seed
 * Safe to re-run: it upserts by a natural key and does not touch patients
 * or appointments.
 */
require("dotenv").config();

const mongoose = require("mongoose");
const connectDB = require("../config/db");
const Clinic = require("../models/Clinic");
const Doctor = require("../models/Doctor");

const seed = async () => {
    await connectDB();

    await Clinic.findOneAndUpdate(
        { clinicName: "ABC Clinic" },
        {
            clinicName: "ABC Clinic",
            timings: "Mon-Sat, 9:00 AM - 5:00 PM",
            address: "123 Health Street, Wellness City",
            phone: "+91-00000-00000",
            slotDuration: 30,
        },
        { upsert: true, returnDocument: "after", setDefaultsOnInsert: true }
    );

    const doctors = [
        {
            name: "Asha Mehta",
            specialization: "General Physician",
            consultationDuration: 30,
            availableDays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            workingHours: { start: "09:00", end: "13:00" },
            isActive: true,
        },
        {
            name: "Rohit Sharma",
            specialization: "Dentist",
            consultationDuration: 20,
            availableDays: ["Monday", "Wednesday", "Friday", "Saturday"],
            workingHours: { start: "14:00", end: "17:00" },
            isActive: true,
        },
    ];

    for (const doc of doctors) {
        await Doctor.findOneAndUpdate(
            { name: doc.name, specialization: doc.specialization },
            doc,
            { upsert: true, returnDocument: "after", setDefaultsOnInsert: true }
        );
    }

    console.log("Seed complete: 1 clinic, 2 doctors.");
    await mongoose.connection.close();
    process.exit(0);
};

seed().catch((err) => {
    console.error("Seed failed:", err);
    process.exit(1);
});
