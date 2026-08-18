let allJobs = [];

const searchInput = document.getElementById("search-input");
const companyFilter = document.getElementById("company-filter");
const atsFilter = document.getElementById("ats-filter");
const tableBody = document.getElementById("jobs-table-body");
const jobCount = document.getElementById("job-count");
const errorMessage = document.getElementById("error-message");


async function loadJobs() {
    try {
        const response = await fetch("data/jobs.json");

        if (!response.ok) {
            throw new Error(
                `Could not load jobs.json: ${response.status}`
            );
        }

        allJobs = await response.json();

        populateCompanyFilter();
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

        return (
            matchesSearch &&
            matchesCompany &&
            matchesAts
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

        cell.colSpan = 5;
        cell.textContent = "No matching jobs found.";

        row.appendChild(cell);
        tableBody.appendChild(row);

        return;
    }

    for (const job of jobs) {
        const row = document.createElement("tr");

        row.appendChild(createCell(job.company));
        row.appendChild(createCell(job.title));
        row.appendChild(createCell(job.location || "Not listed"));
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

loadJobs();