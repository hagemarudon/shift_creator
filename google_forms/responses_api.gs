/**
 * フォームの回答をシフト作成アプリに渡す（Google Apps Script のウェブアプリ）
 *
 * Google Cloud やサービスアカウントを使わずに、アプリから回答を読む・シフトを書き戻すためのもの。
 * 合言葉（key）を知っている人だけが読めます。手順は google_forms/README.md の「アプリに回答を自動で渡す」。
 *
 *   1. 回答が集まるスプレッドシートで「拡張機能 → Apps Script」を開き、ファイルを1つ追加してこれを貼る
 *   2. 関数「合言葉を作る」を実行する
 *   3. 「デプロイ → 新しいデプロイ → ウェブアプリ」（実行するユーザー：自分、アクセスできるユーザー：全員）
 *   4. 出てきたURLの後ろに ?key=合言葉 を付けて、アプリの Secrets の [responses] url に入れる
 */

var PROP_KEY = 'shiftApiKey';

function 合言葉を作る() {
  var key = Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty(PROP_KEY, key);
  var msg = '合言葉: ' + key + '\n\nウェブアプリのURLの後ろに ?key=' + key + ' を付けて、アプリの Secrets に入れてください。'
    + '\n作り直すと前の合言葉は使えなくなります。';
  Logger.log(msg);
  try { SpreadsheetApp.getUi().alert(msg); } catch (e) { /* エディタから実行したときはログだけ */ }
}

function checkKey_(e) {
  var key = PropertiesService.getScriptProperties().getProperty(PROP_KEY);
  return key && e && e.parameter && e.parameter.key === key;
}

function text_(s) {
  return ContentService.createTextOutput(s).setMimeType(ContentService.MimeType.TEXT);
}

function responseSheet_() {
  var sheets = SpreadsheetApp.getActiveSpreadsheet().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (/^(フォームの回答|Form [Rr]esponses)/.test(sheets[i].getName())) return sheets[i];
  }
  for (var j = 0; j < sheets.length; j++) {
    var head = sheets[j].getRange(1, 1, 1, Math.max(1, sheets[j].getLastColumn())).getDisplayValues()[0];
    if (head.some(function (h) { return /氏名|名前|Name/.test(h); })) return sheets[j];
  }
  return null;
}

function csvCell_(v) {
  v = String(v);
  return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}

// 回答をCSVで返す
function doGet(e) {
  if (!checkKey_(e)) return text_('ERROR: 合言葉が違います');
  var sh = responseSheet_();
  if (!sh) return text_('ERROR: フォームの回答のシートが見つかりません');
  var values = sh.getDataRange().getDisplayValues();
  return ContentService.createTextOutput(values.map(function (r) { return r.map(csvCell_).join(','); }).join('\n'))
    .setMimeType(ContentService.MimeType.CSV);
}

// アプリが作ったシフトをシートに書き込む。本文は {"sheets": {"シフト表": [[...], ...], ...}}
function doPost(e) {
  if (!checkKey_(e)) return text_('ERROR: 合言葉が違います');
  var body = JSON.parse(e.postData.contents);
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  Object.keys(body.sheets || {}).forEach(function (name) {
    if (/^(フォームの回答|Form [Rr]esponses)/.test(name)) return; // 回答のシートは上書きしない
    var rows = body.sheets[name];
    var sh = ss.getSheetByName(name) || ss.insertSheet(name);
    sh.clear();
    if (!rows.length) return;
    var width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
    rows = rows.map(function (r) { while (r.length < width) r.push(''); return r; });
    sh.getRange(1, 1, rows.length, width).setValues(rows);
  });
  return text_('OK');
}
