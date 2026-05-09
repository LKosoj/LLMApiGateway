with open('static/editor.js', 'r') as f:
    content = f.read()

# Replace json lint with no lint (or json5 lint if available, but json lint causes errors on valid JSON5)
# Actually, the user says "Отключи строгий linting JSON во фронтенд редакторе (или подключи JSON5 линтер) и добавь подсказку для пользователя о поддержке комментариев."
# It's easier to disable the built-in json linter than add a json5 linter since jsonlint doesn't support json5 without a custom linter setup.

content = content.replace('lint: true, // Use the standard JSON lint addon', 'lint: false, // Disabled standard JSON lint to support JSON5 (comments)')

with open('static/editor.js', 'w') as f:
    f.write(content)

with open('static/rules-editor.html', 'r') as f:
    html = f.read()

# Remove json-lint script
html = html.replace('<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.15/addon/lint/json-lint.min.js"></script>', '<!-- json-lint removed for JSON5 support -->')

# Add hint for comments
hint = '<p class="hint">The backend supports JSON5 format. You can safely use comments (// or /* */) and trailing commas in your configurations.</p>'

html = html.replace('</header>', '</header>\n    ' + hint)

with open('static/rules-editor.html', 'w') as f:
    f.write(html)
