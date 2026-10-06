// Fix ZCode claude-opus-5-5 "requires adaptive thinking" 400 error.
// Replaces the built-in legacy budget-thinking mapping
// (thinking:{type:"enabled",budget_tokens:N}) with effort-only
// providerOptionsByLevel, so ZCode sends output_config.effort and omits
// thinking entirely (adaptive default).
//
// Claude Opus 5.5 rejects:
//   thinking.type = "disabled"                    -> 400
//   thinking.type = "enabled" + budget_tokens     -> 400
// Claude Opus 5.5 accepts:
//   omit thinking (adaptive default) + output_config.effort
//   thinking = {"type":"adaptive"} + output_config.effort
const fs = require('fs');
const path = require('path');

const providerId = 'a7528217-f5c5-4ffd-91d9-b4311a7918b3'; // "ikun claude"
const modelId = 'claude-opus-5-5';
const configPath = path.join(process.env.USERPROFILE, '.zcode', 'v2', 'config.json');

const raw = fs.readFileSync(configPath, 'utf8');
const config = JSON.parse(raw);

const provider = config.provider && config.provider[providerId];
if (!provider) {
  console.error(`Provider ${providerId} not found in ${configPath}`);
  process.exit(1);
}
const model = provider.models && provider.models[modelId];
if (!model) {
  console.error(`Model ${modelId} not found under provider ${providerId}`);
  process.exit(1);
}

// effort levels accepted by claude-opus-5-5 (ZCode jar schema:
// effort: enum(["low","medium","high","xhigh","max"]))
const levels = ['low', 'medium', 'high', 'xhigh', 'max'];
const providerOptionsByLevel = {};
for (const level of levels) {
  // effort-only -> AnthropicMessagesLanguageModel.getArgs() emits
  // output_config:{effort} and no thinking field (adaptive default).
  providerOptionsByLevel[level] = { anthropic: { effort: level } };
}

// Backup before writing
const backupPath = configPath + '.bak-claude-opus-5-5';
fs.copyFileSync(configPath, backupPath);

model.reasoning = {
  enabled: true,
  levels,
  defaultLevel: 'medium',
  providerOptionsByLevel,
};

fs.writeFileSync(configPath, JSON.stringify(config, null, 2) + '\n', 'utf8');

// Validate round-trip
const check = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const r = check.provider[providerId].models[modelId].reasoning;
if (!r || !r.providerOptionsByLevel || Object.keys(r.providerOptionsByLevel).length !== 5) {
  console.error('Validation failed');
  process.exit(1);
}
console.log('OK. Backup at:', backupPath);
console.log('claude-opus-5-5 reasoning is now:');
console.log(JSON.stringify(r, null, 2));
