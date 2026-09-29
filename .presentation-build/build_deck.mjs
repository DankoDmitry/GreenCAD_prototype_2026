import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const workspaceDir = "D:/ЗагрузкиD/GreenCAD_prototype_2026";
const SKILL_DIR = "C:/Users/ddigo/.codex/plugins/cache/openai-primary-runtime/presentations/26.905.11957/skills/presentations";
const TMP_DIR = path.join(workspaceDir, ".presentation-build");
const FINAL_PPTX = path.join(workspaceDir, "presentation", "GreenCAD_Desktop_LCT_2026.pptx");
const RUNTIME_PYTHON = "C:/Users/ddigo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe";

const { resolvePresentationFont, finalizePresentation } = await import(
  pathToFileURL(path.join(SKILL_DIR, "container_tools/artifact_tool_utils.mjs")).href,
);

const fontFamily = resolvePresentationFont();
console.log(`Using presentation font: ${fontFamily}`);

const W = 1280;
const H = 720;
const C = {
  forest: "#123F32",
  forest2: "#1E6A50",
  green: "#2E8B68",
  mint: "#DDECE4",
  pale: "#F4F7F3",
  white: "#FFFFFF",
  ink: "#17251F",
  muted: "#5F7067",
  line: "#CFD9D2",
  amber: "#E5B84B",
  red: "#B74F4F",
};

const screenshotCad = await fs.readFile(
  "C:/Users/ddigo/AppData/Local/Temp/codex-clipboard-e7e62d60-f6dc-4001-839b-c399e2eb8376.png",
);
const screenshotOverview = await fs.readFile(
  "C:/Users/ddigo/AppData/Local/Temp/codex-clipboard-634b1881-d731-4ef5-be98-361df8c7d313.png",
);
const screenshotResult = await fs.readFile(
  "C:/Users/ddigo/AppData/Local/Temp/codex-clipboard-370ebc75-a517-44fe-9c44-cdd4f334bcc2.png",
);

const presentation = Presentation.create({ slideSize: { width: W, height: H } });

function addRect(slide, x, y, width, height, fill, line = "none", radius = 0, shadow = undefined) {
  const shape = slide.shapes.add({
    geometry: "rect",
    position: { left: x, top: y, width, height },
    fill,
    line: line === "none" ? { fill: "none", width: 0 } : { style: "solid", fill: line, width: 1 },
    ...(radius ? { borderRadius: radius } : {}),
    ...(shadow ? { shadow } : {}),
  });
  return shape;
}

function addLine(slide, x, y, width, height, color = C.line, thickness = 1) {
  return slide.shapes.add({
    geometry: "line",
    position: { left: x, top: y, width, height },
    fill: "none",
    line: { style: "solid", fill: color, width: thickness },
  });
}

function addText(slide, text, x, y, width, height, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position: { left: x, top: y, width, height },
    fill: "none",
    line: { fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    typeface: fontFamily,
    fontSize: options.size ?? 20,
    bold: options.bold ?? false,
    color: options.color ?? C.ink,
  };
  shape.text.alignment = options.align ?? "left";
  shape.text.verticalAlignment = options.valign ?? "top";
  shape.text.autoFit = options.autoFit ?? "shrinkText";
  return shape;
}

function addHeader(slide, number, title) {
  slide.background.fill = C.pale;
  addText(slide, "GreenCAD", 56, 28, 180, 30, { size: 18, bold: true, color: C.forest });
  addText(slide, "ЛЦТ 2026", 1070, 30, 150, 26, { size: 14, bold: true, color: C.muted, align: "right" });
  addLine(slide, 56, 66, 1168, 0, C.line, 1);
  addText(slide, title, 56, 84, 1168, 54, { size: 34, bold: true, color: C.ink });
  addText(slide, String(number).padStart(2, "0"), 1160, 681, 64, 20, { size: 12, color: C.muted, align: "right" });
}

function addImageFrame(slide, bytes, alt, x, y, width, height, options = {}) {
  addRect(slide, x - 6, y - 6, width + 12, height + 12, C.white, C.line, 14, "shadow-md");
  return slide.images.add({
    blob: bytes,
    contentType: "image/png",
    alt,
    fit: options.fit ?? "contain",
    position: { left: x, top: y, width, height },
    geometry: "roundRect",
    borderRadius: 10,
    ...(options.crop ? { crop: options.crop } : {}),
  });
}

function addSection(slide, heading, body, x, y, width, accent = C.green) {
  addRect(slide, x, y + 4, 6, 56, accent, "none", 3);
  addText(slide, heading, x + 22, y, width - 22, 28, { size: 20, bold: true, color: C.forest });
  addText(slide, body, x + 22, y + 31, width - 22, 64, { size: 16, color: C.muted });
}

// Slide 1: cover
{
  const slide = presentation.slides.add();
  slide.background.fill = C.forest;
  addText(slide, "Команда «Не работаем с утками»", 70, 58, 560, 34, {
    size: 16,
    bold: true,
    color: C.mint,
  });
  addText(slide, "GreenCAD\nDesktop", 70, 162, 660, 160, {
    size: 56,
    bold: true,
    color: C.white,
  });
  addText(slide, "Автоматизированное проектирование\nозеленения на основе DXF", 70, 338, 710, 98, {
    size: 28,
    color: C.mint,
  });
  addRect(slide, 70, 472, 168, 5, C.amber, "none", 2);
  addText(slide, "Локальный редактор. Проверяемые посадки. Экспорт в CAD.", 70, 503, 760, 58, {
    size: 19,
    color: C.white,
  });
  addText(slide, "0.6.1", 900, 132, 310, 150, { size: 104, bold: true, color: C.forest2, align: "right" });
  addText(slide, "ЛЦТ 2026", 930, 290, 280, 46, { size: 26, bold: true, color: C.amber, align: "right" });
  addText(slide, "Москва, 2026", 70, 651, 260, 28, { size: 14, color: C.mint });
  slide.speakerNotes.textFrame.setText(
    "Основной прототип: GreenCAD Desktop 0.6.1. Команда «Не работаем с утками». Источник: локальный репозиторий greencad_desktop_v1.",
  );
}

// Slide 2: problem and product
{
  const slide = presentation.slides.add();
  addHeader(slide, 2, "Задача и продукт");
  addSection(slide, "Исходные данные", "Типизированный DXF с границами, зданиями и инженерными сетями.", 58, 164, 390);
  addSection(slide, "Работа в GreenCAD", "Проектные типы, карта допустимости, авторасстановка и ручная корректировка.", 58, 294, 390, C.amber);
  addSection(slide, "Результат", "DXF с отдельными слоями посадок, а также отчёт о проверках и координатах.", 58, 424, 390);
  addImageFrame(slide, screenshotOverview, "Общий план полного коридора 3-й Парковой в GreenCAD", 500, 154, 716, 404);
  addRect(slide, 500, 586, 716, 54, C.mint, "none", 10);
  addText(slide, "Исходные слои сохраняются. Новые объекты создаются на отдельных слоях.", 522, 600, 674, 28, {
    size: 16,
    bold: true,
    color: C.forest,
    align: "center",
  });
  slide.speakerNotes.textFrame.setText(
    "Скриншот предоставлен пользователем. Файл: codex-clipboard-634b1881-d731-4ef5-be98-361df8c7d313.png.",
  );
}

// Slide 3: workflow
{
  const slide = presentation.slides.add();
  addHeader(slide, 3, "Рабочий процесс");
  addLine(slide, 112, 236, 1046, 0, C.line, 3);
  const steps = [
    ["1", "Открытие DXF", "XDATA или точное сопоставление слоёв задают смысл объектов."],
    ["2", "Типы посадок", "Радиус, каталог растений и параметры проекта остаются редактируемыми."],
    ["3", "Допустимость", "Выбранные проверки формируют карту разрешённых и запрещённых зон."],
    ["4", "Авторасстановка", "Генератор создаёт вариант на свободной территории с заданным шагом."],
    ["5", "Контроль и экспорт", "Специалист правит результат, повторяет проверки и сохраняет DXF."],
  ];
  const xs = [60, 306, 552, 798, 1044];
  for (let i = 0; i < steps.length; i++) {
    const [n, heading, body] = steps[i];
    const x = xs[i];
    addRect(slide, x + 78, 206, 58, 58, i === 3 ? C.amber : C.green, "none", 29);
    addText(slide, n, x + 78, 214, 58, 36, { size: 23, bold: true, color: i === 3 ? C.ink : C.white, align: "center", valign: "middle" });
    addText(slide, heading, x, 292, 214, 58, { size: 20, bold: true, color: C.forest, align: "center" });
    addText(slide, body, x, 356, 214, 132, { size: 15, color: C.muted, align: "center" });
  }
  addRect(slide, 124, 548, 1032, 70, C.mint, "none", 12);
  addText(slide, "Generator и модуль проверок разделены. Каждая посадка имеет ID, тип и координаты.", 154, 568, 972, 30, {
    size: 18,
    bold: true,
    color: C.forest,
    align: "center",
  });
  slide.speakerNotes.textFrame.setText(
    "Поток основан на фактическом UI GreenCAD Desktop 0.6.1 и модулях generation/checks.",
  );
}

// Slide 4: result
{
  const slide = presentation.slides.add();
  addHeader(slide, 4, "Результат на полном коридоре 3-й Парковой");
  addImageFrame(slide, screenshotResult, "400 посадок и итог выбранных проверок в GreenCAD", 52, 145, 822, 463);
  const mx = 928;
  addText(slide, "17 026", mx, 150, 284, 62, { size: 42, bold: true, color: C.forest });
  addText(slide, "исходных объектов", mx, 207, 284, 32, { size: 16, color: C.muted });
  addLine(slide, mx, 250, 284, 0, C.line, 1);
  addText(slide, "400", mx, 266, 284, 60, { size: 42, bold: true, color: C.green });
  addText(slide, "посадок в варианте", mx, 321, 284, 32, { size: 16, color: C.muted });
  addLine(slide, mx, 363, 284, 0, C.line, 1);
  addText(slide, "400 / 0", mx, 379, 284, 60, { size: 40, bold: true, color: C.forest });
  addText(slide, "прошло / нарушено", mx, 434, 284, 32, { size: 16, color: C.muted });
  addLine(slide, mx, 476, 284, 0, C.line, 1);
  addText(slide, "0", mx, 492, 284, 58, { size: 40, bold: true, color: C.green });
  addText(slide, "ошибок выбранных проверок", mx, 545, 284, 48, { size: 15, color: C.muted });
  addText(slide, "Фактические значения с экрана GreenCAD 0.6.1. Нормативная приёмка требует подтверждённых правил и источников.", 58, 636, 1110, 36, {
    size: 13,
    color: C.muted,
  });
  slide.speakerNotes.textFrame.setText(
    "Скриншот предоставлен пользователем. Файл: codex-clipboard-370ebc75-a517-44fe-9c44-cdd4f334bcc2.png. Метрики считаны с интерфейса на скриншоте.",
  );
}

// Slide 5: manual editing
{
  const slide = presentation.slides.add();
  addHeader(slide, 5, "Ручная корректировка и повторная проверка");
  addText(slide, "Автоматический вариант остаётся редактируемым проектом. Специалист может доработать каждую посадку.", 58, 150, 650, 74, {
    size: 23,
    color: C.ink,
  });
  const actions = [
    ["Переместить", "Координаты X и Y меняются мышью или через панель свойств."],
    ["Копировать или удалить", "Копия получает новый ID. Удаление и отмена работают из интерфейса."],
    ["Изменить тип и метку", "Проектный тип, название и привязка остаются доступными для ручной правки."],
    ["Повторить проверку", "Команды «Проверить место» и «Проверить допуски» пересчитывают выбранные условия."],
  ];
  let yy = 254;
  for (const [heading, body] of actions) {
    addRect(slide, 58, yy + 8, 12, 12, C.green, "none", 6);
    addText(slide, heading, 86, yy, 430, 28, { size: 19, bold: true, color: C.forest });
    addText(slide, body, 86, yy + 30, 540, 54, { size: 15, color: C.muted });
    yy += 92;
  }

  addRect(slide, 810, 220, 220, 220, C.mint, C.green, 110);
  addRect(slide, 854, 264, 132, 132, C.white, C.green, 66);
  addText(slide, "T1", 854, 298, 132, 48, { size: 34, bold: true, color: C.green, align: "center", valign: "middle" });
  addText(slide, "посадка", 854, 348, 132, 28, { size: 14, color: C.muted, align: "center" });
  addText(slide, "движение", 1036, 245, 160, 28, { size: 17, bold: true, color: C.forest });
  addText(slide, "свойства", 1036, 315, 160, 28, { size: 17, bold: true, color: C.forest });
  addText(slide, "проверки", 1036, 385, 160, 28, { size: 17, bold: true, color: C.forest });
  addLine(slide, 1000, 258, 30, 0, C.green, 2);
  addLine(slide, 1000, 328, 30, 0, C.green, 2);
  addLine(slide, 1000, 398, 30, 0, C.green, 2);
  addRect(slide, 740, 504, 470, 104, C.forest, "none", 12);
  addText(slide, "Ручные посадки сохраняются при новой авторасстановке. Экспорт берёт актуальную геометрию.", 772, 529, 406, 62, {
    size: 18,
    bold: true,
    color: C.white,
    align: "center",
    valign: "middle",
  });
  slide.speakerNotes.textFrame.setText(
    "Функции ручного редактирования взяты из GreenCAD Desktop 0.6.1: выбор/перенос, копия, удаление, панель свойств, проверка места и допусков.",
  );
}

// Slide 6: CAD export
{
  const slide = presentation.slides.add();
  addHeader(slide, 6, "Экспорт обратно в CAD");
  addImageFrame(
    slide,
    screenshotCad,
    "Экспортированный DXF с посадками, открытый в AutoCAD",
    52,
    145,
    824,
    411,
    { fit: "contain", crop: { left: 0, top: 0, right: 0.16, bottom: 0 } },
  );
  addText(slide, "Файл открывается в AutoCAD", 78, 577, 770, 30, { size: 18, bold: true, color: C.forest, align: "center" });
  addText(slide, "PASS", 936, 156, 250, 58, { size: 40, bold: true, color: C.green });
  addText(slide, "сохранение исходных сущностей", 936, 210, 250, 54, { size: 16, color: C.muted });
  addLine(slide, 936, 280, 250, 0, C.line, 1);
  addText(slide, "PASS", 936, 296, 250, 58, { size: 40, bold: true, color: C.green });
  addText(slide, "проверка координат при экспорте", 936, 350, 250, 54, { size: 16, color: C.muted });
  addLine(slide, 936, 420, 250, 0, C.line, 1);
  addText(slide, "Отдельные слои", 936, 442, 250, 34, { size: 22, bold: true, color: C.forest });
  addText(slide, "Исходная CAD-геометрия остаётся на месте. Посадки записываются как новые объекты.", 936, 480, 250, 110, { size: 16, color: C.muted });
  addText(slide, "Статусы взяты из паспорта экспорта полного коридора 3-й Парковой.", 58, 638, 1120, 30, { size: 13, color: C.muted });
  slide.speakerNotes.textFrame.setText(
    "Скриншот предоставлен пользователем. Файл: codex-clipboard-e7e62d60-f6dc-4001-839b-c399e2eb8376.png. Статусы PASS: materials/dxf_examples/02_3ya_Parkovaya_typed_full_corridor_edited.export.json.",
  );
}

// Slide 7: MVP status
{
  const slide = presentation.slides.add();
  addHeader(slide, 7, "Что готово в MVP");
  addText(slide, "Работает сейчас", 68, 156, 510, 38, { size: 25, bold: true, color: C.forest });
  const ready = [
    "Локальное приложение для Windows и Linux",
    "Импорт типизированного DXF и сохранение проекта",
    "Авторасстановка нескольких типов посадок",
    "Ручной перенос, копирование и редактирование",
    "Проверки, отчёты и экспорт DXF для CAD",
  ];
  let y = 216;
  for (const item of ready) {
    addRect(slide, 72, y + 5, 12, 12, C.green, "none", 6);
    addText(slide, item, 100, y, 500, 52, { size: 18, color: C.ink });
    y += 73;
  }

  addText(slide, "Границы текущей версии", 680, 156, 520, 38, { size: 25, bold: true, color: C.forest });
  const limits = [
    "Исходный DWG сначала нужно подготовить и экспортировать в DXF",
    "Тестовая зелёная зона в демо не заменяет инженерное подтверждение",
    "Каждое нормативное расстояние требует источника и версии",
    "Итоговое проектное решение принимает профильный специалист",
  ];
  y = 216;
  for (const item of limits) {
    addRect(slide, 684, y + 5, 12, 12, C.amber, "none", 6);
    addText(slide, item, 712, y, 500, 62, { size: 18, color: C.ink });
    y += 88;
  }
  addRect(slide, 0, 628, 1280, 92, C.forest, "none");
  addText(slide, "github.com/DankoDmitry/greencad_desktop_v1", 66, 650, 760, 34, { size: 22, bold: true, color: C.white });
  addText(slide, "Команда «Не работаем с утками»", 798, 654, 416, 30, { size: 15, color: C.mint, align: "right" });
  slide.speakerNotes.textFrame.setText(
    "Готовые функции и ограничения основаны на README и экспортных отчётах greencad_desktop_v1 0.6.1.",
  );
}

await fs.mkdir(TMP_DIR, { recursive: true });
await fs.mkdir(path.dirname(FINAL_PPTX), { recursive: true });
const stagingDir = path.join(workspaceDir, ".codex-finalizer");
await fs.mkdir(stagingDir, { recursive: true });
const candidatePath = path.join(stagingDir, "GreenCAD_Desktop_LCT_2026.candidate.pptx");
await (await PresentationFile.exportPptx(presentation)).save(candidatePath);

const requirements = {
  explicitTotalSlideCount: 7,
  requiredNativeTableOwnerSlides: [],
  requiredNativeChartOwnerSlides: [],
};
const fontPolicy = { basis: "design", families: [fontFamily] };
const result = await finalizePresentation({
  ...requirements,
  workspaceDir,
  candidatePath,
  finalPath: FINAL_PPTX,
  pythonExecutable: RUNTIME_PYTHON,
  integrityValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs: [
    "--expected-slide-size-emu",
    "12192000,6858000",
    "--validate-bullet-geometry",
    "--validate-heading-fit",
  ],
  requiredNativeTableOwnerSlides: [],
  fontPolicy,
  verifyArtifactToolImport: true,
  receiptPath: path.join(stagingDir, "GreenCAD_Desktop_LCT_2026.validation.json"),
});

console.log(JSON.stringify({ final: FINAL_PPTX, result }, null, 2));
