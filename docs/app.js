import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
    getAuth,
    GoogleAuthProvider,
    signInWithPopup,
    signOut,
    onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { getFirestore, collection, getDocs, doc, getDoc } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";

let allJobs = [];
let selectedMajor = "";

// Keep this list in sync with MAJOR_TAGS in gemini_extract.py
const MAJORS = [
    "Aerospace Engineering",
    "Applied Sciences in Engineering",
    "Biomedical Engineering",
    "Chemical Engineering",
    "Civil Engineering",
    "Electrical Engineering",
    "Computer Engineering",
    "Energy Systems Engineering",
    "Environmental Engineering",
    "Industrial Engineering",
    "Materials Science and Engineering",
    "Mechanical Engineering",
    "Packaging Engineering",
    "General/Other",
];

const searchInput = document.getElementById("search-input");
const companyFilter = document.getElementById("company-filter");
const atsFilter = document.getElementById("ats-filter");
const majorFilters = document.getElementById("major-filters");
const tableBody = document.getElementById("jobs-table-body");
const jobCount = document.getElementById("job-count");
const errorMessage = document.getElementById("error-message");

const authGate = document.getElementById("auth-gate");
const authError = document.getElementById("auth-error");
const appContent = document.getElementById("app-content");
const signInButton = document.getElementById("sign-in-button");
const signOutButton = document.getElementById("sign-out-button");
const signedInAs = document.getElementById("signed-in-as");


// ---- Firebase setup ----
// Auth and Firestore both use the modular SDK now, not a mix of compat
// and modular. Compat (loaded as a classic <script> from CDN) and
// modular (loaded via ES import) turned out to be two separate SDK
// bundles that don't share the same app registry, which is what broke
// the earlier version of this file: firebase.initializeApp() (compat)
// created an app that modular getApp() couldn't see at all. Using one
// consistent API end to end avoids that entirely.
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);

// Firestore needs the modular API regardless, since only it supports
// connecting to a named database. Ours is named "default" (not the
// reserved "(default)").
const db = getFirestore(app, "default");


// Access is limited to emails listed in the Firestore "allowlist"
// collection (document ID = lowercase email). firestore.rules enforces
// this on the server; this check only decides what the page shows.
async function isAllowedEmail(user) {
    if (!user || !user.emailVerified || typeof user.email !== "string") {
        return false;
    }
    try {
        const entry = await getDoc(doc(db, "allowlist", user.email.toLowerCase()));
        return entry.exists();
    } catch (error) {
        // permission-denied means the email is not on the allowlist
        console.error(error);
        return false;
    }
}


signInButton.addEventListener("click", () => {
    authError.textContent = "";

    const provider = new GoogleAuthProvider();
    // Always show the account picker so people can switch accounts.
    provider.setCustomParameters({ prompt: "select_account" });

    signInWithPopup(auth, provider).catch(error => {
        console.error(error);
        authError.textContent = "Sign-in failed. Please try again.";
    });
});


signOutButton.addEventListener("click", () => {
    signOut(auth);
});


onAuthStateChanged(auth, async user => {
    if (!user) {
        appContent.hidden = true;
        authGate.hidden = false;
        return;
    }

    if (!(await isAllowedEmail(user))) {
        authError.textContent =
            `${user.email} doesn't have access yet. Contact the SHE IVP AI & Tech Committee to be added.`;
        signOut(auth);
        return;
    }

    authGate.hidden = true;
    appContent.hidden = false;
    signedInAs.textContent = `Signed in as ${user.email}`;

    loadJobs();
});


async function loadJobs() {
    try {
        const snapshot = await getDocs(collection(db, "jobs"));

        allJobs = snapshot.docs.map(doc => doc.data());

        populateCompanyFilter();
        renderMajorFilterButtons();
        renderJobs(allJobs);
    } catch (error) {
        console.error(error);

        jobCount.textContent = "Unable to load jobs.";
        errorMessage.textContent =
            "The internship listings could not be loaded.";
    }
}


function populateCompanyFilter() {
    const companies = [
        ...new Set(
            allJobs
                .map(job => job.company)
                .filter(Boolean)
        )
    ].sort();

    for (const company of companies) {
        const option = document.createElement("option");

        option.value = company;
        option.textContent = company;

        companyFilter.appendChild(option);
    }
}


function renderMajorFilterButtons() {
    majorFilters.innerHTML = "";

    const allButton = document.createElement("button");
    allButton.type = "button";
    allButton.className = "major-btn active";
    allButton.textContent = "All majors";
    allButton.addEventListener("click", () => selectMajor("", allButton));
    majorFilters.appendChild(allButton);

    for (const major of MAJORS) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "major-btn";
        button.textContent = major;
        button.addEventListener("click", () => selectMajor(major, button));
        majorFilters.appendChild(button);
    }
}


function selectMajor(major, clickedButton) {
    selectedMajor = major;

    for (const button of majorFilters.querySelectorAll(".major-btn")) {
        button.classList.toggle("active", button === clickedButton);
    }

    filterJobs();
}


function filterJobs() {
    const searchTerm = searchInput.value.trim().toLowerCase();
    const selectedCompany = companyFilter.value;
    const selectedAts = atsFilter.value;

    const filteredJobs = allJobs.filter(job => {
        const searchableText = [
            job.company,
            job.title,
            job.location,
            job.department,
            job.team
        ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();

        const matchesSearch =
            !searchTerm || searchableText.includes(searchTerm);

        const matchesCompany =
            !selectedCompany ||
            job.company === selectedCompany;

        const matchesAts =
            !selectedAts ||
            job.ats === selectedAts;

        const jobMajors = Array.isArray(job.majors) ? job.majors : [];

        const matchesMajor =
            !selectedMajor ||
            jobMajors.includes(selectedMajor);

        return (
            matchesSearch &&
            matchesCompany &&
            matchesAts &&
            matchesMajor
        );
    });

    renderJobs(filteredJobs);
}


function renderJobs(jobs) {
    tableBody.innerHTML = "";

    jobCount.textContent =
        `${jobs.length} internship-like position` +
        `${jobs.length === 1 ? "" : "s"} found`;

    if (jobs.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");

        cell.colSpan = 6;
        cell.textContent = "No matching jobs found.";

        row.appendChild(cell);
        tableBody.appendChild(row);

        return;
    }

    for (const job of jobs) {
        const row = document.createElement("tr");

        const majorsText = Array.isArray(job.majors) && job.majors.length
            ? job.majors.join(", ")
            : "Not tagged";

        row.appendChild(createCell(job.company));
        row.appendChild(createCell(job.title));
        row.appendChild(createCell(job.location || "Not listed"));
        row.appendChild(createCell(majorsText));
        row.appendChild(createCell(job.ats || "Unknown"));

        const linkCell = document.createElement("td");
        const link = document.createElement("a");

        link.href = job.apply_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Apply";

        linkCell.appendChild(link);
        row.appendChild(linkCell);

        tableBody.appendChild(row);
    }
}


function createCell(value) {
    const cell = document.createElement("td");
    cell.textContent = value || "";
    return cell;
}


searchInput.addEventListener("input", filterJobs);
companyFilter.addEventListener("change", filterJobs);
atsFilter.addEventListener("change", filterJobs);