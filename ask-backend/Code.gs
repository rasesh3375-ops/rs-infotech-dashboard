/**
 * Ask your data -- the small server behind the dashboard's Ask box.
 *
 * The dashboard works every figure out itself, in the browser, from what
 * the signed-in person may read; this script only holds the Claude API key
 * (which can't sit in the public page) and passes each step of the
 * conversation to Claude. Before it does, it checks the request comes from
 * the owner or an active staff login -- the same people the Firestore rules
 * let read the data -- so nobody else can spend the key.
 *
 * Setup, once (about five minutes):
 *  1. script.google.com > New project, named "RS Infotech Ask". Paste this
 *     file over Code.gs and save.
 *  2. Project Settings (gear) > Script Properties > Add property:
 *     ANTHROPIC_API_KEY = the key from console.anthropic.com. Save.
 *  3. Deploy > New deployment > type Web app. Execute as: Me.
 *     Who has access: Anyone. Deploy, allow the permissions it asks for,
 *     and copy the Web app URL (ending /exec) -- it goes into ASK_URL in
 *     index.html.
 * A later change to this file: Deploy > Manage deployments > edit > New
 * version, which keeps the same URL.
 */

const ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages';
const MODEL = 'claude-sonnet-5-5';
const MAX_TOKENS = 2000;
// The dashboard's own Firebase web key -- public by design, it is in the
// page too. Used only to ask Firebase whose sign-in token this is.
const FIREBASE_API_KEY = 'AIzaSyDO1xPB4d4AD1UbEkzaArIPYLHHD9MGzr8';
const FIREBASE_PROJECT = 'rs-infotech-dashboard';
const OWNER_EMAIL = 'rasesh3375@gmail.com';
const MAX_REQUEST_CHARS = 300000;
// Steps per login per day: a question takes two to four. Keeps a stuck page
// or a mistake from running up the bill.
const DAILY_STEPS = 300;

function doPost(e) {
  try {
    const body = (e && e.postData && e.postData.contents) || '';
    if (body.length > MAX_REQUEST_CHARS) return reply_({ ok: false, error: 'That conversation is too long -- close Ask and open it again.' });
    const req = JSON.parse(body);
    const who = signedIn_(req.idToken);
    if (!who) return reply_({ ok: false, error: 'Only the dashboard\'s own logins can use Ask. Sign in again and retry.' });
    if (!withinDailyLimit_(who)) return reply_({ ok: false, error: 'Ask has reached today\'s limit for this login. It resets tomorrow.' });
    const key = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
    if (!key) return reply_({ ok: false, error: 'The AI key hasn\'t been added to the Ask script yet.' });
    const res = UrlFetchApp.fetch(ANTHROPIC_URL, {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01' },
      payload: JSON.stringify({ model: MODEL, max_tokens: MAX_TOKENS, system: String(req.system || '').slice(0, 10000),
                                tools: Array.isArray(req.tools) ? req.tools : [], messages: req.messages || [] }),
    });
    const out = JSON.parse(res.getContentText() || '{}');
    if (res.getResponseCode() !== 200) {
      return reply_({ ok: false, error: 'The AI service said no (' + res.getResponseCode() + '): ' + ((out.error || {}).message || 'no reason given') });
    }
    return reply_({ ok: true, message: { content: out.content || [], stop_reason: out.stop_reason } });
  } catch (err) {
    return reply_({ ok: false, error: 'Ask script error: ' + err });
  }
}

// The owner, or a users/{uid} document with active == true -- read with the
// person's own token, so Firestore's rules decide, exactly as on the page.
function signedIn_(idToken) {
  if (!idToken) return null;
  const look = UrlFetchApp.fetch('https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=' + FIREBASE_API_KEY, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true, payload: JSON.stringify({ idToken: idToken }) });
  if (look.getResponseCode() !== 200) return null;
  const user = (JSON.parse(look.getContentText()).users || [])[0];
  if (!user) return null;
  if (String(user.email || '').toLowerCase() === OWNER_EMAIL) return user.localId;
  const doc = UrlFetchApp.fetch('https://firestore.googleapis.com/v1/projects/' + FIREBASE_PROJECT +
    '/databases/(default)/documents/users/' + encodeURIComponent(user.localId), {
    headers: { Authorization: 'Bearer ' + idToken }, muteHttpExceptions: true });
  if (doc.getResponseCode() !== 200) return null;
  const fields = JSON.parse(doc.getContentText()).fields || {};
  // A "Serial number search only" login (role 'serials') is active too, but may
  // use nothing but the Replacement app's serial search -- not Ask.
  const role = (fields.role || {}).stringValue || 'staff';
  return (fields.active || {}).booleanValue === true && role === 'staff' ? user.localId : null;
}

function withinDailyLimit_(uid) {
  const cache = CacheService.getScriptCache();
  const k = 'steps_' + uid + '_' + Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyyMMdd');
  const n = Number(cache.get(k) || 0) + 1;
  cache.put(k, String(n), 6 * 3600);
  return n <= DAILY_STEPS;
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
