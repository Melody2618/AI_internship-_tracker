// Public Firebase web config. This is safe to commit and safe to expose,
// it is not a secret. Access is enforced by Authentication + firestore.rules,
// not by hiding this file.
//
// Fill these in from:
// Firebase console > Project settings > General > Your apps > SDK setup and configuration

const firebaseConfig = {
    apiKey: "AIzaSyDY_Y3GpiAaROaxmZJ6Oms2eTxgLcFzY1c",
    authDomain: "she-internship-tracker.firebaseapp.com",
    projectId: "she-internship-tracker",
    storageBucket: "she-internship-tracker.firebasestorage.app",
    messagingSenderId: "205048407129",
    appId: "1:205048407129:web:ac317c56ca75859531fd48",
    measurementId: "G-EFZYKTVE93"
  };

// Restrict Google Sign-In to Rutgers ScarletMail accounts at the login
// screen. This is a UX hint only (Google honors it, but a determined user
// could bypass it), the real restriction is in firestore.rules.
const ALLOWED_EMAIL_DOMAIN = "scarletmail.rutgers.edu";