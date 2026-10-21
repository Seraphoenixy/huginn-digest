from integration.http.llm import complete

class TextModel:
    def __init__(self, settings):
        self.settings = settings

    def call(self, system, text, json_mode=False):
        return complete(self.settings, [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}], json_mode=json_mode)
