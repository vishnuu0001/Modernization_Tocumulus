const HIDDEN_MODEL_NAMES = /qwen\s*3\.5\s*[:\- ]?\s*9b/i;
const DEEPSEEK_CODER_67B = /deepseek[\s-]*coder\s*[:\- ]?\s*6\.7b/i;
const DEVSTRAL_SMALL_2_24B = /devstral[\s-]*small[\s-]*2\s*[:\- ]?\s*24b/i;

export const modelDisplayName = (value, fallback = 'OpenSource LLM') => {
  const text = String(value || '').trim();
  if (!text || HIDDEN_MODEL_NAMES.test(text)) return fallback;
  if (DEVSTRAL_SMALL_2_24B.test(text)) return 'Devstral Small 2 24B';
  if (DEEPSEEK_CODER_67B.test(text)) return 'DeepSeek-Coder 6.7B';
  return text;
};
