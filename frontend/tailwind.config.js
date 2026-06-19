/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: {
    extend: {
      colors: {
        background: '#0a0a0a',
        surface: '#141414',
        border: '#262626',
        accent: '#3b82f6',
      },
    },
  },
  plugins: [],
};
