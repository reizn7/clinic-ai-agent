const axios = require("axios");

/**
 * WhatsApp Cloud API service.
 *
 * This is the ONLY place in the codebase that talks to the Graph API.
 * The axios request below is a 1:1 move of the call that previously
 * lived inline in routes/webhook.js — same endpoint, same headers
 * (Bearer ACCESS_TOKEN), same payload shape — so sending behaviour is
 * unchanged. Nothing else should import axios to reach WhatsApp.
 */

/**
 * Send a plain text WhatsApp message.
 * @param {string} phone recipient in wa_id / E.164 form (message.from)
 * @param {string} text  message body
 * @returns {Promise<object>} Graph API response data
 */
const sendTextMessage = async (phone, text) => {
    const url = `https://graph.facebook.com/${process.env.GRAPH_API_VERSION}/${process.env.PHONE_NUMBER_ID}/messages`;

    const payload = {
        messaging_product: "whatsapp",
        recipient_type: "individual",
        to: phone,
        type: "text",
        text: {
            preview_url: false,
            body: text,
        },
    };

    const { data } = await axios.post(url, payload, {
        headers: {
            Authorization: `Bearer ${process.env.ACCESS_TOKEN}`,
            "Content-Type": "application/json",
        },
    });

    return data;
};

module.exports = { sendTextMessage };
