const mongoose = require("mongoose");

/**
 * Connect to MongoDB Atlas using MONGODB_URI from .env.
 *
 * Called (and awaited) before Express starts listening so the app never
 * serves traffic without a database. On failure we log and exit so the
 * process manager / ngrok setup can restart cleanly rather than run in a
 * broken state.
 */
const connectDB = async () => {
    if (!process.env.MONGODB_URI) {
        console.error("Missing MONGODB_URI in environment. Cannot start.");
        process.exit(1);
    }

    try {
        const conn = await mongoose.connect(process.env.MONGODB_URI);
        console.log(`MongoDB Connected: ${conn.connection.host}`);
    } catch (err) {
        console.error("MongoDB connection error:", err.message);
        process.exit(1);
    }

    // Surface post-connection issues instead of failing silently.
    mongoose.connection.on("error", (err) => {
        console.error("MongoDB runtime error:", err.message);
    });
    mongoose.connection.on("disconnected", () => {
        console.warn("MongoDB disconnected.");
    });
};

module.exports = connectDB;
