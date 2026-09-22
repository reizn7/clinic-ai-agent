/**
 * Operational (expected) error carrying an HTTP status code.
 * Throw these from services/controllers; the error middleware
 * turns them into clean JSON responses.
 */
class ApiError extends Error {
    constructor(statusCode, message, details = undefined) {
        super(message);
        this.name = "ApiError";
        this.statusCode = statusCode;
        this.details = details;
        this.isOperational = true;
        Error.captureStackTrace(this, this.constructor);
    }

    static badRequest(message, details) {
        return new ApiError(400, message, details);
    }

    static notFound(message = "Resource not found") {
        return new ApiError(404, message);
    }

    static conflict(message) {
        return new ApiError(409, message);
    }
}

module.exports = ApiError;
