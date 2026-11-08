/**
 * バイトのシフト作成：出勤できる時間を集める Google フォーム（Google Apps Script）
 *
 * 使い方（くわしくは google_forms/README.md）
 *   1. 新しい Google スプレッドシートを作り、「拡張機能 → Apps Script」にこのファイルを丸ごと貼る
 *   2. 下の「設定」をお店と期間に合わせて直し、関数「フォームを作る」を実行する
 *      → フォームができ、回答はこのスプレッドシートの「フォームの回答 1」シートに集まる
 *   3. 回答が集まったら、アプリでこのスプレッドシートを読み込む（またはCSVでダウンロードしてアップロード）
 */

// ===== 設定 =====
var CONFIG = {
  shop: '〇〇店',
  // シフトの期間（この期間の日付を1週間ずつ聞く）
  startDate: '2026-11-16',
  endDate: '2026-11-29',
  // 聞く時間帯と刻み（分）。アプリの slot_minutes と同じにする
  openTime: '10:00',
  closeTime: '22:00',
  slotMinutes: 30,
  // 定休日（曜日。例 ['水']）。この曜日は選択肢に出さない
  closedWeekdays: [],
};

// 質問の見出し（アプリは「氏名」「最大日数」「希望勤務時間」「備考」を含む見出しで回答を探す）
var Q = {
  name: '氏名',
  maxDays: '期間中に入れる最大日数（任意）',
  target: '期間中の希望勤務時間（任意）',
  avail: '出勤できる時間',
  note: '備考（任意）',
};
var WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];
var PROP_FORM = 'shiftFormId';

function onOpen() {
  SpreadsheetApp.getUi().createMenu('シフト')
    .addItem('フォームを作る', 'フォームを作る')
    .addItem('フォームのURLを表示', 'フォームのURLを表示')
    .addToUi();
}

function フォームを作る() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var props = PropertiesService.getDocumentProperties();
  if (props.getProperty(PROP_FORM) && !confirm_('フォームはもう作ってあります。新しく作り直しますか？（前のフォームと回答は残ります）')) {
    return;
  }
  var days = dates_();
  var form = FormApp.create(CONFIG.shop + ' シフト希望（' + md_(days[0]) + '〜' + md_(days[days.length - 1]) + '）');
  form.setDescription('出勤できる時間に○を付けてください（' + CONFIG.slotMinutes + '分ごと）。'
    + '答え直すときは、送信後の「回答を編集」から直すか、同じ氏名でもう一度送ってください（最後の回答を使います）。');
  form.setAllowResponseEdits(true);
  form.addTextItem().setTitle(Q.name).setRequired(true);
  var whole = FormApp.createTextValidation().requireWholeNumber().setHelpText('数字で入力してください').build();
  var num = FormApp.createTextValidation().requireNumber().setHelpText('数字で入力してください').build();
  form.addTextItem().setTitle(Q.maxDays).setValidation(whole);
  form.addTextItem().setTitle(Q.target).setHelpText('時間で（例：15）').setValidation(num);
  weeks_(days).forEach(function (week) {
    form.addCheckboxGridItem()
      .setTitle(Q.avail + '（' + md_(week[0]) + '〜' + md_(week[week.length - 1]) + '）')
      .setRows(timeLabels_())
      .setColumns(week.map(dayLabel_));
  });
  form.addParagraphTextItem().setTitle(Q.note);
  form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());
  props.setProperty(PROP_FORM, form.getId());
  フォームのURLを表示();
}

function フォームのURLを表示() {
  var id = PropertiesService.getDocumentProperties().getProperty(PROP_FORM);
  var msg = id
    ? '回答用（スタッフに送るURL）\n' + FormApp.openById(id).getPublishedUrl() + '\n\n編集用\n' + FormApp.openById(id).getEditUrl()
    : 'まだフォームがありません。メニュー「シフト → フォームを作る」を押してください。';
  Logger.log(msg);
  try { SpreadsheetApp.getUi().alert(msg); } catch (e) { /* エディタから実行したときはログだけ */ }
}

// ===== 下請け =====
function dates_() {
  var s = parseDate_(CONFIG.startDate), e = parseDate_(CONFIG.endDate), out = [];
  for (var d = new Date(s); d <= e; d.setDate(d.getDate() + 1)) {
    if (CONFIG.closedWeekdays.indexOf(WEEKDAYS[d.getDay()]) < 0) out.push(new Date(d));
  }
  if (!out.length) throw new Error('期間に日付がありません。startDate と endDate を確かめてください。');
  return out;
}

// 月曜はじまりで1週間ずつに分ける
function weeks_(days) {
  var out = [], cur = [];
  days.forEach(function (d) {
    if (cur.length && d.getDay() === 1) { out.push(cur); cur = []; }
    cur.push(d);
  });
  if (cur.length) out.push(cur);
  return out;
}

function timeLabels_() {
  var s = toMin_(CONFIG.openTime), e = toMin_(CONFIG.closeTime), out = [];
  for (var t = s; t < e; t += CONFIG.slotMinutes) out.push(hhmm_(t) + '-' + hhmm_(t + CONFIG.slotMinutes));
  return out;
}

function parseDate_(s) { var p = s.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]); }
function md_(d) { return (d.getMonth() + 1) + '/' + d.getDate(); }
function dayLabel_(d) { return md_(d) + '(' + WEEKDAYS[d.getDay()] + ')'; }
function toMin_(s) { var p = s.split(':'); return +p[0] * 60 + +p[1]; }
function hhmm_(m) { return Math.floor(m / 60) + ':' + ('0' + (m % 60)).slice(-2); }

function confirm_(msg) {
  try {
    var ui = SpreadsheetApp.getUi();
    return ui.alert(msg, ui.ButtonSet.YES_NO) === ui.Button.YES;
  } catch (e) {
    return true;
  }
}
