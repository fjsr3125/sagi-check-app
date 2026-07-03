const TOKEN_PROPERTY = 'SAGI_OPERATOR_TOKEN';

function doPost(e) {
  try {
    const body = JSON.parse(e.postData && e.postData.contents ? e.postData.contents : '{}');
    verifyToken_(body.token || '');

    const action = String(body.action || '');
    if (action === 'get') {
      return json_({
        ok: true,
        values: SpreadsheetApp.openById(body.spreadsheetId).getRange(body.range).getValues(),
      });
    }
    if (action === 'update') {
      SpreadsheetApp.openById(body.spreadsheetId).getRange(body.range).setValues(body.values || []);
      return json_({ ok: true });
    }
    if (action === 'metadata') {
      const sheets = SpreadsheetApp.openById(body.spreadsheetId).getSheets().map((sheet) => ({
        properties: {
          title: sheet.getName(),
          sheetId: sheet.getSheetId(),
        },
      }));
      return json_({ ok: true, metadata: { sheets } });
    }
    return json_({ ok: false, error: `unknown action: ${action}` });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

function verifyToken_(token) {
  const expected = PropertiesService.getScriptProperties().getProperty(TOKEN_PROPERTY);
  if (!expected) {
    throw new Error(`${TOKEN_PROPERTY} is not configured`);
  }
  if (token !== expected) {
    throw new Error('invalid token');
  }
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
