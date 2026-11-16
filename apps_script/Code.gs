/**
 * SIJK CHECK — private Google Sheet bridge (bound Apps Script).
 *
 * Deploy: Web app, Execute as: Me, Who has access: Anyone.
 * IMPORTANT: "Anyone" applies to the SCRIPT ENDPOINT, not to the spreadsheet.
 * Set Script Property SIJK_CHECK_API_KEY to a long random secret.
 * The bot sends it only in the HTTPS POST JSON body, never in a URL.
 *
 * The endpoint supports ONLY lookup by exact Riot ID and append-if-absent.
 * No listing, updating existing rows, deleting, or changing sharing settings.
 */
const SIJK_CHECK_SHEET_NAME = 'База игроков';
const SIJK_CHECK_SPREADSHEET_ID = '19Lhc8VIDr6eCL4wHf-jUR3JyjOIdpDHbk7LeXxs9ANY';
const SIJK_CHECK_STATUSES = [
  'Да', 'Вероятнее да', 'Под вопросом', 'Вероятнее нет', 'Нет'
];

function doGet() {
  return reply_({ok: false, error: 'POST_ONLY'});
}

function reply_(data) {
  return ContentService.createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}

function safeCell_(value, maxLength) {
  const text = String(value == null ? '' : value).slice(0, maxLength);
  // Prevent user-controlled values from becoming spreadsheet formulas.
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function validRiot_(riotId) {
  return typeof riotId === 'string' && riotId.length <= 50 &&
    /^[^#\r\n]{1,32}#[^#\r\n]{1,16}$/.test(riotId) &&
    riotId.split('#').every(part => part.trim().length > 0);
}

function normalize_(riotId) {
  return riotId.trim().toLowerCase();
}

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents ||
        e.postData.contents.length > 50000) {
      return reply_({ok: false, error: 'BAD_REQUEST'});
    }
    const payload = JSON.parse(e.postData.contents);
    const expected = PropertiesService.getScriptProperties().getProperty('SIJK_CHECK_API_KEY');
    if (!expected || expected.length < 32 ||
        typeof payload.key !== 'string' || payload.key !== expected) {
      return reply_({ok: false, error: 'UNAUTHORIZED'});
    }
    if (!validRiot_(payload.riot_id)) {
      return reply_({ok: false, error: 'INVALID_RIOT_ID'});
    }
    if (payload.action !== 'lookup' && payload.action !== 'add') {
      return reply_({ok: false, error: 'UNKNOWN_ACTION'});
    }

    const lock = LockService.getScriptLock();
    if (!lock.tryLock(15000)) {
      return reply_({ok: false, error: 'BUSY'});
    }
    try {
      const spreadsheet = SpreadsheetApp.openById(SIJK_CHECK_SPREADSHEET_ID);
      const sheet = spreadsheet.getSheetByName(SIJK_CHECK_SHEET_NAME);
      if (!sheet) return reply_({ok: false, error: 'SHEET_NOT_FOUND'});
      const lastRow = sheet.getLastRow();
      const rows = lastRow >= 4
        ? sheet.getRange(4, 1, lastRow - 3, 7).getDisplayValues() : [];
      const key = normalize_(payload.riot_id);
      const existing = rows.find(row => normalize_(row[0]) === key);

      if (payload.action === 'lookup') {
        // Return one matching row only, not the whole private sheet.
        return reply_({ok: true, record: existing || null});
      }

      const score = payload.score;
      const status = payload.status;
      if (!Number.isInteger(score) || score < 1 || score > 10 ||
          !SIJK_CHECK_STATUSES.includes(status) ||
          typeof payload.comment !== 'string' || payload.comment.length > 1000 ||
          typeof payload.evidence !== 'string' || payload.evidence.length > 40000 ||
          typeof payload.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(payload.date)) {
        return reply_({ok: false, error: 'INVALID_EVALUATION'});
      }
      if (existing) return reply_({ok: true, result: 'exists'});

      const tracker = 'https://tracker.gg/valorant/profile/riot/' +
        encodeURIComponent(payload.riot_id) + '/overview';
      const targetRow = Math.max(4, lastRow + 1);
      // Write only A:G; preserve all other cells and existing evaluations.
      sheet.getRange(targetRow, 1, 1, 7).setValues([[
        safeCell_(payload.riot_id, 50),
        score,
        status,
        tracker,
        safeCell_(payload.evidence, 40000),
        safeCell_(payload.comment, 1000),
        payload.date
      ]]);
      SpreadsheetApp.flush();
      return reply_({ok: true, result: 'added'});
    } finally {
      lock.releaseLock();
    }
  } catch (error) {
    console.error('SIJK CHECK bridge error', String(error));
    return reply_({ok: false, error: 'INTERNAL_ERROR'});
  }
}
