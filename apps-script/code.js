// function doGet() {
//   return HtmlService.createHtmlOutputFromFile('index');
// }


/**
 * Send an email with schemas in order to test email markup.
 */
function testSchemas() {
  try {
    const htmlBody = HtmlService.createHtmlOutputFromFile('hotel').getContent();

    MailApp.sendEmail({
      to: Session.getActiveUser().getEmail(),
      subject: 'Hotel reservation update',
      htmlBody: htmlBody
    });
  } catch (err) {
    console.log(err.message);
  }
}


/**
 * Web app endpoint for the gmail-markup MCP server: email the posted markup to myself.
 * Expects JSON {token, subject, htmlBody}; token must match the API_TOKEN script property.
 */
function doPost(e) {
  const req = JSON.parse(e.postData.contents);
  const token = PropertiesService.getScriptProperties().getProperty('API_TOKEN');
  if (!token || req.token !== token) {
    return json_({ok: false, error: 'unauthorised'});
  }

  // getActiveUser() is empty for anonymous web app calls; the effective user is me
  MailApp.sendEmail({
    to: Session.getEffectiveUser().getEmail(),
    subject: req.subject,
    htmlBody: req.htmlBody
  });
  return json_({ok: true});
}


function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}