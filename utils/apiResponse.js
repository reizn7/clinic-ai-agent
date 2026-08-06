/**
 * Standard success envelope so the future React dashboard always
 * receives a predictable shape: { success, message, data }.
 */
const sendSuccess = (res, data = null, message = "OK", statusCode = 200) =>
    res.status(statusCode).json({
        success: true,
        message,
        data,
    });

module.exports = { sendSuccess };
