/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/js/**/*.js"],
  theme: {
    extend: {
      colors: {
        ink: "#111111",
        paper: "#FAF9F6",
        line: "#E4E1DA",
        accent: "#B3492B",
      },
      fontFamily: {
        display: ["'Newsreader'", "Georgia", "serif"],
        sans: [
          "'Inter'",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
