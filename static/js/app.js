const search=document.getElementById("search");

search.addEventListener("keyup",function(){

    let value=this.value.toUpperCase();

    let rows=document.querySelectorAll("#stockTable tbody tr");

    rows.forEach(function(row){

        let text=row.innerText.toUpperCase();

        row.style.display=text.includes(value) ? "" : "none";

    });

});

// Auto Refresh every 60 seconds
setInterval(function(){
    location.reload();
},60000);

