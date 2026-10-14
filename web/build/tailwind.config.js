module.exports = {
  content: ['../index.html','../app.js'],
  plugins: [require('@tailwindcss/forms')],
  theme: { extend: {
    colors: {
      "tertiary-container": "#996100", "on-tertiary-container": "#ffeedd", "primary": "#004ac6",
      "inverse-on-surface": "#eaf1ff", "secondary-fixed-dim": "#4edea3", "outline": "#737686",
      "surface-container-low": "#eff4ff", "secondary": "#006c49", "surface-dim": "#cbdbf5",
      "secondary-fixed": "#6ffbbe", "primary-fixed-dim": "#b4c5ff", "surface-tint": "#0053db",
      "tertiary-fixed-dim": "#ffb95f", "surface": "#f8f9ff", "on-secondary-container": "#00714d",
      "surface-container-highest": "#d3e4fe", "primary-container": "#2563eb", "on-error-container": "#93000a",
      "tertiary-fixed": "#ffddb8", "surface-container": "#e5eeff", "surface-container-lowest": "#ffffff",
      "surface-container-high": "#dce9ff", "error-container": "#ffdad6", "primary-fixed": "#dbe1ff",
      "on-surface-variant": "#434655", "on-surface": "#0b1c30", "tertiary": "#784b00",
      "secondary-container": "#6cf8bb", "error": "#ba1a1a", "on-primary": "#ffffff", "outline-variant": "#c3c6d7"
    },
    borderRadius: { DEFAULT: "0.125rem", lg: "0.25rem", xl: "0.5rem", "2xl": "1rem", "3xl": "1.5rem", full: "9999px" },
    spacing: { "space-xs": "0.25rem", "space-sm": "0.5rem", "space-md": "1rem", "space-lg": "1.5rem", "space-xl": "2rem", gutter: "1.5rem" },
    fontFamily: { sans: ["Inter", "sans-serif"] },
    fontSize: {
      "headline-xl": ["36px", { lineHeight: "44px", letterSpacing: "-0.025em", fontWeight: "700" }],
      "headline-lg": ["24px", { lineHeight: "32px", letterSpacing: "-0.02em", fontWeight: "600" }],
      "headline-sm": ["18px", { lineHeight: "26px", letterSpacing: "-0.01em", fontWeight: "600" }],
      "body-lg": ["16px", "24px"], "body-md": ["14px", "20px"], "body-sm": ["12px", "16px"],
      "label-numeric": ["20px", { lineHeight: "24px", fontWeight: "600" }],
      "label-caps": ["11px", { lineHeight: "14px", letterSpacing: "0.06em", fontWeight: "600" }],
      "label-sm": ["12px", { lineHeight: "16px", letterSpacing: "0.02em", fontWeight: "500" }]
    }
  } }
};
