/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/hansard_annotator/web/templates/**/*.html",
    "./src/hansard_annotator/web/static/src/**/*.js"
  ],
  theme: {
    extend: {
      colors: {
        canvas: "#f7f7f4",
        ink: "#20232a",
        muted: "#626875",
        brand: { DEFAULT: "#3f46a5", dark: "#303783" }
      },
      boxShadow: { paper: "0 8px 30px rgba(28,33,52,.08)" }
    }
  },
  plugins: []
};
