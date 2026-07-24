/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./backend/app/templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "sans-serif",
        ],
      },
      colors: {
        // Ember: warm amber accent on soft neutral surfaces.
        ember: {
          50: "#fff8ed",
          100: "#ffefd4",
          200: "#ffdaa8",
          300: "#ffbe71",
          400: "#ff9838",
          500: "#fd7c12",
          600: "#ee6008",
          700: "#c54709",
          800: "#9c3810",
          900: "#7e3010",
          950: "#441606",
        },
        charcoal: {
          50: "#f6f6f5",
          100: "#e7e7e5",
          200: "#d1d0cd",
          300: "#b1afaa",
          400: "#8a8781",
          500: "#6f6c66",
          600: "#5e5b56",
          700: "#504e49",
          800: "#454340",
          900: "#3d3b39",
          950: "#26251f",
        },
        sand: {
          50: "#fbfaf8",
          100: "#f5f2ec",
          200: "#ebe6dc",
          300: "#ded6c6",
          400: "#c9bda6",
        },
      },
      boxShadow: {
        card: "0 1px 2px rgba(38, 37, 31, 0.05), 0 4px 16px rgba(38, 37, 31, 0.04)",
        lift: "0 2px 4px rgba(38, 37, 31, 0.06), 0 12px 32px rgba(38, 37, 31, 0.10)",
      },
      borderRadius: {
        xl: "0.75rem",
        "2xl": "1rem",
      },
    },
  },
  plugins: [],
};
