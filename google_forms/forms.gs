/**
 * バイトのシフト作成：出勤できる時間を集める Google フォーム（Google Apps Script）
 *
 * 使い方（くわしくは google_forms/README.md）
 *   1. 新しい Google スプレッドシートを作り、「拡張機能 → Apps Script」にこのファイルを丸ごと貼る
 *   2. スプレッドシートに「名簿」シートを作り、A列（2行目から）にスタッフの氏名を書く
 *      （無ければ「フォームを作る」を実行したときに空の「名簿」シートができるので、書いてからもう一度実行）
 *   3. 下の「設定」をお店と期間に合わせて直し、関数「フォームを作る」を実行する
 *      → フォームができ、回答はこのスプレッドシートの「フォームの回答 1」シートに集まる
 *      → 氏名は名簿から選ぶプルダウンになるので、アプリの氏名と必ず一致する
 *   名簿を直したら「名簿をフォームに反映」を実行する（前のスクリプトで作った、氏名が記述式のフォームもプルダウンに変わる）
 *   3. 回答が集まったら、アプリでこのスプレッドシートを読み込む（またはCSVでダウンロードしてアップロード）
 */

// ===== 設定 =====
var CONFIG = {
  shop: '〇〇店',
  // シフトの期間（この期間の日付を1週間ずつ聞く）
  startDate: '2026-11-16',
  endDate: '2026-11-29',
  // 聞く時間帯と刻み（分）。アプリの時間刻みと同じか細かくする（フォーム30分・アプリ60分なら、1時間ずっと出勤できる人だけ入れる）
  openTime: '10:00',
  closeTime: '22:00',
  slotMinutes: 30,
  // 定休日（曜日。例 ['水']）。この曜日は選択肢に出さない
  closedWeekdays: [],
  // スタッフの氏名を書いておくシート（A列の2行目から）。フォームの氏名はここから選ぶプルダウンになる
  rosterSheet: '名簿',
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
    .addItem('名簿をフォームに反映', '名簿をフォームに反映')
    .addItem('フォームのURLを表示', 'フォームのURLを表示')
    .addToUi();
}

function フォームを作る() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var props = PropertiesService.getDocumentProperties();
  if (props.getProperty(PROP_FORM) && !confirm_('フォームはもう作ってあります。新しく作り直しますか？（前のフォームと回答は残ります）')) {
    return;
  }
  var names = roster_();
  if (!names) return;
  var days = dates_();
  var form = FormApp.create(CONFIG.shop + ' シフト希望（' + md_(days[0]) + '〜' + md_(days[days.length - 1]) + '）');
  form.setDescription('出勤できる時間に○を付けてください（' + CONFIG.slotMinutes + '分ごと）。'
    + '答え直すときは、送信後の「回答を編集」から直すか、同じ氏名でもう一度送ってください（最後の回答を使います）。');
  form.setAllowResponseEdits(true);
  form.addListItem().setTitle(Q.name).setChoiceValues(names).setRequired(true).setHelpText(NAME_HELP);
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

// 名簿の氏名を、フォームの氏名のプルダウンに入れ直す。氏名が記述式なら、同じ位置のプルダウンに置きかえる
function 名簿をフォームに反映() {
  var form = form_();
  if (!form) return alert_('フォームが見つかりません。先にメニュー「シフト → フォームを作る」を押してください。');
  var names = roster_();
  if (!names) return;
  var items = form.getItems();
  var list = null, text = null;
  items.forEach(function (it) {
    if (it.getTitle() !== Q.name) return;
    if (it.getType() === FormApp.ItemType.LIST) list = it.asListItem();
    else if (it.getType() === FormApp.ItemType.TEXT) text = it;
  });
  var msg;
  if (list) {
    list.setChoiceValues(names);
    msg = 'フォームの氏名の選択肢を名簿の ' + names.length + '人にしました。';
  } else {
    var at = text ? text.getIndex() : 0;
    list = form.addListItem().setTitle(Q.name).setChoiceValues(names).setRequired(true).setHelpText(NAME_HELP);
    if (text) form.deleteItem(text);
    form.moveItem(list.getIndex(), at);
    msg = 'フォームの氏名を、名簿の ' + names.length + '人から選ぶプルダウンにしました。'
      + '\nこれまでの回答はそのまま使えます（回答のシートに新しい「氏名」の列が増えますが、アプリは両方の列を読みます）。';
  }
  alert_(msg);
}

function フォームのURLを表示() {
  var form = form_();
  var msg = form
    ? '回答用（スタッフに送るURL）\n' + form.getPublishedUrl() + '\n\n編集用\n' + form.getEditUrl()
    : 'まだフォームがありません。メニュー「シフト → フォームを作る」を押してください。';
  alert_(msg);
}

// ===== 名簿 =====
var NAME_HELP = '名前がないときは、シフトを作る人に名簿へ追加してもらってください。';

// 名簿の氏名（空白と重複は除く）。名簿が無い・空のときは空の名簿シートを用意して知らせ、null を返す
function roster_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(CONFIG.rosterSheet);
  if (!sh) {
    sh = ss.insertSheet(CONFIG.rosterSheet);
    sh.getRange(1, 1).setValue('氏名').setFontWeight('bold');
  }
  var seen = {}, names = [];
  if (sh.getLastRow() >= 2) {
    sh.getRange(2, 1, sh.getLastRow() - 1, 1).getDisplayValues().forEach(function (r) {
      var n = String(r[0]).trim();
      var key = n.replace(/\s+/g, '');
      if (n && !seen[key]) { seen[key] = true; names.push(n); }
    });
  }
  if (!names.length) {
    alert_('「' + CONFIG.rosterSheet + '」シートのA列（2行目から）にスタッフの氏名を書いてから、もう一度実行してください。');
    return null;
  }
  return names;
}

// このスクリプトで作ったフォーム。無ければ、このスプレッドシートに回答が届くフォーム
function form_() {
  var id = PropertiesService.getDocumentProperties().getProperty(PROP_FORM);
  if (id) return FormApp.openById(id);
  var url = SpreadsheetApp.getActiveSpreadsheet().getFormUrl();
  return url ? FormApp.openByUrl(url) : null;
}

function alert_(msg) {
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

// 最初の日から7日ずつに分ける（7日以内なら1問にまとまる）
function weeks_(days) {
  var out = [];
  var first = days[0].getTime();
  days.forEach(function (d) {
    var i = Math.floor((d.getTime() - first) / (7 * 24 * 3600 * 1000));
    (out[i] = out[i] || []).push(d);
  });
  return out.filter(function (w) { return w; });
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
