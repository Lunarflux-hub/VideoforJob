from aiogram.fsm.state import State, StatesGroup





class MiscFlow(StatesGroup):
    waiting_new_email = State()  # смена email из профиля
    waiting_support_message = State()
    waiting_review_comment = State()  # комментарий к оценке результата (bot/handlers/review.py)


class VideoFlow(StatesGroup):
    """FSM состояния мастера редактирования видео VideoLab AI."""
    waiting_purpose = State()
    waiting_video = State()
    choosing_category = State()
    choosing_preset = State()
    waiting_custom_prompt = State()
    confirm = State()
    waiting_feedback = State()
