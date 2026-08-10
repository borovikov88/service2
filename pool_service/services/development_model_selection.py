import re

from pool_service.services.ai_costs import codex_cost_estimate

COMPLEXITY_MODELS = {"simple": "gpt-5.6-luna", "standard": "gpt-5.6-terra", "complex": "gpt-5.6-sol"}
MODE_MODELS = {"economy": "gpt-5.6-luna", "standard": "gpt-5.6-terra", "maximum": "gpt-5.6-sol"}
MODE_CHOICES = (("auto", "Авто"), ("economy", "Эконом"), ("standard", "Стандарт"), ("maximum", "Максимум"))
ALLOWED_MODELS = frozenset(COMPLEXITY_MODELS.values())
CLASSIFIER_VERSION = "development-task-risk-v1"
COMPLEXITY_LABELS = {"simple": "Простая", "standard": "Обычная", "complex": "Сложная"}
MODEL_LABELS = {"gpt-5.6-luna": "GPT-5.6 Luna", "gpt-5.6-terra": "GPT-5.6 Terra", "gpt-5.6-sol": "GPT-5.6 Sol"}
MODE_LABELS = dict(MODE_CHOICES)
_CLASSIFICATION_RE = re.compile(r"^AUTO_COMPLEXITY:\s*(simple|standard|complex)\s*$", re.I | re.M)
_REASON_RE = re.compile(r"^AUTO_REASON:\s*(.+?)\s*$", re.I | re.M)
_COMPLEX_RISKS = ("security", "безопасност", "permission", "разрешени", "tenant", "изоляц", "migration", "миграц", "data integrity", "целостност", "финанс", "payment", "платеж", "concurren", "конкурент", "race condition", "архитектур", "cross-module", "межмодуль", "критичн")
_SIMPLE_SIGNALS = ("текст", "опечат", "template", "шаблон", " ui ", "интерфейс", "label", "подпис", "небольш", "локальн", "простое исправление", "simple fix", " css ", "верстк")


class ModelSelectionError(ValueError):
    pass


def _reason(value, limit=300):
    return " ".join(str(value or "").split())[:limit].rstrip()


def classify_task(task, analysis_text=""):
    match = _CLASSIFICATION_RE.search(analysis_text or "")
    if match:
        reason_match = _REASON_RE.search(analysis_text or "")
        return match.group(1).lower(), _reason(reason_match.group(1) if reason_match else "Оценено первичным AI-анализом.")
    source = " ".join(str(value or "") for value in (task.title, task.description, task.business_goal, task.definition_of_done)).lower()
    if any(signal in source for signal in _COMPLEX_RISKS):
        return "complex", "Задача затрагивает области повышенного риска или архитектурные изменения."
    if any(signal in source for signal in _SIMPLE_SIGNALS):
        return "simple", "Локальное изменение без признаков архитектурного или критичного риска."
    return "standard", "Обычная задача разработки без признаков высокой критичности."


def validate_model(model):
    if model not in ALLOWED_MODELS:
        raise ModelSelectionError("Модель Codex отсутствует в серверном allowlist.")
    return model


def effective_model(mode, auto_model):
    if mode == "auto":
        model = auto_model
    elif mode in MODE_MODELS:
        model = MODE_MODELS[mode]
    else:
        raise ModelSelectionError("Неизвестный режим выбора модели.")
    return validate_model(model)


def selection_metadata(task, analysis_text=""):
    metadata = dict(task.automation_metadata) if isinstance(task.automation_metadata, dict) else {}
    complexity = metadata.get("auto_complexity")
    auto_model = metadata.get("auto_selected_model")
    if complexity in COMPLEXITY_MODELS and auto_model == COMPLEXITY_MODELS[complexity]:
        reason = _reason(metadata.get("classification_reason")) or "Классификация сохранена ранее."
    else:
        complexity, reason = classify_task(task, analysis_text)
        auto_model = COMPLEXITY_MODELS[complexity]
    mode = metadata.get("model_selection_mode", "auto")
    if mode not in dict(MODE_CHOICES):
        mode = "auto"
    model = effective_model(mode, auto_model)
    metadata.update(auto_complexity=complexity, auto_selected_model=auto_model, classification_reason=reason,
                    classifier_version=metadata.get("classifier_version") or CLASSIFIER_VERSION,
                    model_selection_mode=mode, effective_model=model)
    metadata["codex_cost_estimate"] = codex_cost_estimate(complexity, model)
    return metadata


def display_context(task):
    metadata = task.automation_metadata if isinstance(task.automation_metadata, dict) else {}
    complexity = metadata.get("auto_complexity")
    model = metadata.get("effective_model")
    mode = metadata.get("model_selection_mode", "auto")
    return {"complexity_label": COMPLEXITY_LABELS.get(complexity, "Ещё не определена"),
            "model_label": MODEL_LABELS.get(model, "Ещё не выбрана"),
            "reason": metadata.get("classification_reason") or "Классификация появится после первичного AI-анализа.",
            "mode_label": MODE_LABELS.get(mode, "Авто")}
