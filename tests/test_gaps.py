import pytest

def test_page_fill_is_open_gap():
    from src.newswire import page_fill
    with pytest.raises(NotImplementedError):
        page_fill.process({"id": "x"})

def test_retract_is_open_gap():
    from src.newswire import retract
    with pytest.raises(NotImplementedError):
        retract.process({"id": "x"})
